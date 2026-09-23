"""Extendible Hashing en disco (índice no agrupado ``<clave, RID>``).

Archivo ``<tabla>_<col>.hsh``:

* Página 0 (``FILE_META``): ``global_depth``, ``max_depth``, tipo y tamaño de
  clave, capacidad de bucket, nº de entradas y la tabla de segmentos del
  directorio.
* Directorio (páginas ``HASH_DIR``): arreglo de ``2^global_depth`` punteros i32 a
  buckets, ``E = (B - 24) // 4`` entradas por página. La entrada i vive en la
  página de directorio ``i // E``. Las páginas del directorio se reservan en
  segmentos contiguos (uno por cada duplicación que necesita más páginas), así
  que ubicar una entrada no cuesta lecturas extra: leerla cuesta 1 lectura.
* Bucket (``HASH_BUCKET``): ``flags`` = profundidad local, ``record_count`` = nº de
  pares, ``next_page_id`` = bucket de overflow. Cuerpo: pares
  ``<key, RID>`` contiguos (``cap = (B - 24) // (key_size + 6)``).

Función hash determinista: ``zlib.crc32`` de los bytes empaquetados de la clave
(nunca ``hash()`` de Python). Se usan los bits bajos: ``dir[h & (2^gd - 1)]``.

Inserción: si el bucket está lleno y ``local_depth < global_depth`` se divide y
se actualizan los punteros del directorio; si son iguales primero se duplica el
directorio. Cuando la profundidad llega a ``max_depth`` o todas las claves del
bucket tienen el mismo hash (p. ej. claves repetidas), se encadena un bucket de
overflow. Búsqueda: 1 (directorio) + 1 (bucket) + overflow. No soporta rangos.
Borrado: se quita el par moviendo el último a su lugar (sin fusionar buckets).
"""

from __future__ import annotations

import os
import struct
import zlib
from typing import Any, Iterator

from backend.files.common import DuplicateKeyError
from backend.storage.disk_manager import DiskCounter, DiskManager
from backend.storage.page import (
    NULL_PAGE,
    PAGE_HEADER_SIZE,
    FileKind,
    MetaCodec,
    PageFormatError,
    PageHeader,
    PageType,
    expect_page_type,
)
from backend.storage.record import Column, ColumnType, KeyCodec, encode_str
from backend.storage.rid import RID, RID_SIZE, RID_STRUCT

DEFAULT_MAX_DEPTH = 20
MAX_SEGMENTS = 24
_POINTER = struct.Struct("<i")

HASH_META = MetaCodec(
    FileKind.HASH,
    [
        ("global_depth", "B"),
        ("max_depth", "B"),
        ("key_type", "B"),
        ("unique", "B"),
        ("key_size", "H"),
        ("bucket_capacity", "H"),
        ("dir_entries_per_page", "H"),
        ("entry_count", "Q"),
        ("bucket_pages", "I"),
        ("segment_count", "B"),
        ("seg_first_index", f"{MAX_SEGMENTS}I"),
        ("seg_first_page", f"{MAX_SEGMENTS}i"),
        ("seg_pages", f"{MAX_SEGMENTS}I"),
    ],
)


def bucket_capacity(page_size: int, key_size: int) -> int:
    return (page_size - PAGE_HEADER_SIZE) // (key_size + RID_SIZE)


def dir_entries_per_page(page_size: int) -> int:
    return (page_size - PAGE_HEADER_SIZE) // 4


class _Bucket:
    """Bucket en memoria: sus pares ``<clave, RID>`` crudos y contiguos en ``body``."""

    __slots__ = ("page_id", "local_depth", "body", "next_page")

    def __init__(self, page_id: int, local_depth: int, body: bytes = b"", next_page: int = NULL_PAGE) -> None:
        self.page_id = page_id
        self.local_depth = local_depth
        self.body = body
        self.next_page = next_page

    def entries(self, entry_size: int) -> list[bytes]:
        body = self.body
        return [body[i : i + entry_size] for i in range(0, len(body), entry_size)]

    def key_positions(self, key: bytes, entry_size: int) -> Iterator[int]:
        """Offsets de los pares cuya clave es ``key`` (búsqueda en C alineada al par)."""
        body = self.body
        pos = body.find(key)
        while pos != -1:
            if pos % entry_size == 0:
                yield pos
            pos = body.find(key, pos + 1)


class ExtendibleHash:
    def __init__(self, dm: DiskManager, column: Column, meta: dict[str, Any]) -> None:
        self.dm = dm
        self.column = column
        self.codec = KeyCodec(column)
        self.key_size = self.codec.size
        self.entry_size = self.key_size + RID_SIZE
        self.bucket_capacity = bucket_capacity(dm.page_size, self.key_size)
        self.entries_per_page = dir_entries_per_page(dm.page_size)
        if self.bucket_capacity < 2:
            raise PageFormatError(f"page_size {dm.page_size} demasiado pequeño para claves de {self.key_size} bytes")
        if int(meta["key_size"]) != self.key_size or int(meta["key_type"]) != self.codec.type_code:
            raise PageFormatError(f"{dm.path.name}: el tipo de clave no coincide con el archivo")
        self.global_depth = int(meta["global_depth"])
        self.max_depth = int(meta["max_depth"])
        self.unique = bool(meta["unique"])
        self.entry_count = int(meta["entry_count"])
        self.bucket_pages = int(meta["bucket_pages"])
        n = int(meta["segment_count"])
        self.segments: list[tuple[int, int, int]] = [
            (meta["seg_first_index"][i], meta["seg_first_page"][i], meta["seg_pages"][i])  # type: ignore[index]
            for i in range(n)
        ]
        self._dir_struct = struct.Struct(f"<{self.entries_per_page}i")
        self._is_char = column.type == ColumnType.CHAR
        self._is_float = column.type == ColumnType.FLOAT
        self._dirty = False
        self._stats_dirty = False

    # ------------------------------------------------------------------ apertura
    @classmethod
    def create(
        cls,
        path: str | os.PathLike[str],
        column: Column,
        page_size: int,
        counter: DiskCounter,
        *,
        unique: bool = False,
        max_depth: int = DEFAULT_MAX_DEPTH,
    ) -> ExtendibleHash:
        dm = DiskManager(path, page_size, counter, create=True)
        codec = KeyCodec(column)
        meta_page = dm.allocate_page()
        dir_page = dm.allocate_page()
        bucket_page = dm.allocate_page()
        meta: dict[str, Any] = {
            "global_depth": 0,
            "max_depth": max_depth,
            "key_type": codec.type_code,
            "unique": 1 if unique else 0,
            "key_size": codec.size,
            "bucket_capacity": bucket_capacity(page_size, codec.size),
            "dir_entries_per_page": dir_entries_per_page(page_size),
            "entry_count": 0,
            "bucket_pages": 1,
            "segment_count": 1,
            "seg_first_index": [0] * MAX_SEGMENTS,
            "seg_first_page": [dir_page] + [NULL_PAGE] * (MAX_SEGMENTS - 1),
            "seg_pages": [1] + [0] * (MAX_SEGMENTS - 1),
        }
        h = cls(dm, column, meta)
        entries = [NULL_PAGE] * h.entries_per_page
        entries[0] = bucket_page
        h._write_dir_page(0, entries)
        h._write_bucket(_Bucket(bucket_page, 0))
        dm.write_page(meta_page, h._meta_bytes())
        return h

    @classmethod
    def open(cls, path: str | os.PathLike[str], column: Column, page_size: int, counter: DiskCounter) -> ExtendibleHash:
        dm = DiskManager(path, page_size, counter)
        stored_ps, _, meta = HASH_META.unpack(dm.read_page(0))
        if stored_ps != page_size:
            dm.close()
            raise PageFormatError(f"{dm.path.name}: page_size {stored_ps} != {page_size}")
        return cls(dm, column, meta)

    def _meta_bytes(self) -> bytes:
        seg_first_index = [0] * MAX_SEGMENTS
        seg_first_page = [NULL_PAGE] * MAX_SEGMENTS
        seg_pages = [0] * MAX_SEGMENTS
        for i, (first_index, first_page, count) in enumerate(self.segments):
            seg_first_index[i], seg_first_page[i], seg_pages[i] = first_index, first_page, count
        return HASH_META.pack(
            self.dm.page_size,
            self.dm.num_pages(),
            {
                "global_depth": self.global_depth,
                "max_depth": self.max_depth,
                "key_type": self.codec.type_code,
                "unique": 1 if self.unique else 0,
                "key_size": self.key_size,
                "bucket_capacity": self.bucket_capacity,
                "dir_entries_per_page": self.entries_per_page,
                "entry_count": self.entry_count,
                "bucket_pages": self.bucket_pages,
                "segment_count": len(self.segments),
                "seg_first_index": seg_first_index,
                "seg_first_page": seg_first_page,
                "seg_pages": seg_pages,
            },
        )

    def flush(self) -> None:
        """Escribe la página 0 si cambió la profundidad global o el directorio."""
        if self._dirty:
            self.dm.write_page(0, self._meta_bytes())
            self._dirty = self._stats_dirty = False

    def sync(self) -> None:
        if self._dirty or self._stats_dirty:
            self.dm.write_page(0, self._meta_bytes())
            self._dirty = self._stats_dirty = False
        self.dm.flush()

    def close(self) -> None:
        if not self.dm.closed:
            self.sync()
            self.dm.close()

    # ------------------------------------------------------------------ estadísticas
    @property
    def page_count(self) -> int:
        return self.dm.num_pages() - 1

    @property
    def dir_pages(self) -> int:
        return sum(count for _, _, count in self.segments)

    # ------------------------------------------------------------------ claves y hash
    def _key_bytes(self, value: Any) -> bytes:
        if self._is_char:
            return self.codec.struct.pack(encode_str(value, self.key_size))
        if self._is_float:
            value = float(value)
            if value == 0.0:
                value = 0.0  # -0.0 y 0.0 son la misma clave
        return self.codec.struct.pack(value)

    @staticmethod
    def hash_bytes(key: bytes) -> int:
        return zlib.crc32(key) & 0xFFFFFFFF

    def _entry(self, key: bytes, rid: RID) -> bytes:
        return key + RID_STRUCT.pack(rid.page_id, rid.slot)

    # ------------------------------------------------------------------ directorio
    def _dir_page_id(self, dir_index: int) -> int:
        for first_index, first_page, count in self.segments:
            if first_index <= dir_index < first_index + count:
                return first_page + (dir_index - first_index)
        raise PageFormatError(f"página de directorio {dir_index} no asignada")

    def _read_dir_page(self, dir_index: int) -> list[int]:
        data = self.dm.read_page(self._dir_page_id(dir_index))
        expect_page_type(PageHeader.unpack(data), PageType.HASH_DIR)
        return list(self._dir_struct.unpack_from(data, PAGE_HEADER_SIZE))

    def _write_dir_page(self, dir_index: int, entries: list[int]) -> None:
        page_id = self._dir_page_id(dir_index)
        used = max(0, min(self.entries_per_page, (1 << self.global_depth) - dir_index * self.entries_per_page))
        buf = bytearray(self.dm.page_size)
        PageHeader(
            page_id=page_id,
            page_type=PageType.HASH_DIR,
            flags=self.global_depth,
            record_count=used,
            free_space_offset=PAGE_HEADER_SIZE + 4 * used,
            aux_page_id=dir_index,
        ).pack_into(buf)
        self._dir_struct.pack_into(buf, PAGE_HEADER_SIZE, *entries)
        self.dm.write_page(page_id, buf)

    def _dir_pages_needed(self, global_depth: int) -> int:
        return -(-(1 << global_depth) // self.entries_per_page)

    def _lookup(self, h: int) -> int:
        """Bucket primario de un hash: 1 lectura de una página del directorio."""
        index = h & ((1 << self.global_depth) - 1)
        data = self.dm.read_page(self._dir_page_id(index // self.entries_per_page))
        return _POINTER.unpack_from(data, PAGE_HEADER_SIZE + 4 * (index % self.entries_per_page))[0]

    def _double_directory(self) -> None:
        """global_depth += 1: la mitad nueva del directorio copia a la mitad vieja.

        Se recorre en streaming: cada página vieja se lee una vez y cada página
        destino se escribe una vez (páginas nuevas en un segmento contiguo).
        """
        if len(self.segments) >= MAX_SEGMENTS:
            raise PageFormatError("tabla de segmentos del directorio llena")
        old_n = 1 << self.global_depth
        new_n = old_n << 1
        per_page = self.entries_per_page
        old_pages = self._dir_pages_needed(self.global_depth)
        new_pages = self._dir_pages_needed(self.global_depth + 1)
        if new_pages > old_pages:
            first = self.dm.allocate_page()
            for _ in range(new_pages - old_pages - 1):
                self.dm.allocate_page()
            self.segments.append((old_pages, first, new_pages - old_pages))
        self.global_depth += 1
        self._dirty = True
        cache: dict[int, list[int]] = {}

        def source(i: int) -> int:
            p = i // per_page
            if p not in cache:
                if len(cache) > 2:
                    cache.pop(next(iter(cache)))
                cache[p] = self._read_dir_page(p)
            return cache[p][i % per_page]

        for p in range(old_n // per_page, new_pages):
            if p < old_pages:
                entries = cache.get(p) or self._read_dir_page(p)
                cache[p] = entries
            else:
                entries = [NULL_PAGE] * per_page
            lo, hi = max(p * per_page, old_n), min((p + 1) * per_page, new_n)
            for i in range(lo, hi):
                entries[i - p * per_page] = source(i - old_n)
            self._write_dir_page(p, entries)

    def _redirect(self, pattern: int, local_depth: int, new_bucket: int) -> None:
        """Tras dividir un bucket de profundidad ``local_depth``, las entradas del
        directorio con esos bits bajos y el bit ``local_depth`` en 1 apuntan al nuevo."""
        step = 1 << (local_depth + 1)
        start = (pattern & ((1 << local_depth) - 1)) | (1 << local_depth)
        per_page = self.entries_per_page
        current_page = -1
        entries: list[int] = []
        for i in range(start, 1 << self.global_depth, step):
            p = i // per_page
            if p != current_page:
                if current_page >= 0:
                    self._write_dir_page(current_page, entries)
                current_page, entries = p, self._read_dir_page(p)
            entries[i % per_page] = new_bucket
        if current_page >= 0:
            self._write_dir_page(current_page, entries)

    # ------------------------------------------------------------------ buckets
    def _read_bucket(self, page_id: int) -> _Bucket:
        data = self.dm.read_page(page_id)
        header = PageHeader.unpack(data)
        expect_page_type(header, PageType.HASH_BUCKET)
        body = data[PAGE_HEADER_SIZE : PAGE_HEADER_SIZE + header.record_count * self.entry_size]
        return _Bucket(page_id, header.flags, body, header.next_page_id)

    def _count(self, bucket: _Bucket) -> int:
        return len(bucket.body) // self.entry_size

    def _write_bucket(self, bucket: _Bucket) -> None:
        body = bucket.body
        buf = bytearray(self.dm.page_size)
        PageHeader(
            page_id=bucket.page_id,
            page_type=PageType.HASH_BUCKET,
            flags=bucket.local_depth,
            record_count=len(body) // self.entry_size,
            free_space_offset=PAGE_HEADER_SIZE + len(body),
            next_page_id=bucket.next_page,
        ).pack_into(buf)
        buf[PAGE_HEADER_SIZE : PAGE_HEADER_SIZE + len(body)] = body
        self.dm.write_page(bucket.page_id, buf)

    def _chain(self, first: int) -> Iterator[_Bucket]:
        page_id = first
        while page_id != NULL_PAGE:
            bucket = self._read_bucket(page_id)
            yield bucket
            page_id = bucket.next_page

    def _new_overflow_after(self, primary: _Bucket, body: bytes) -> None:
        """Encadena un bucket de overflow justo después del primario (2W)."""
        page_id = self.dm.allocate_page()
        self._write_bucket(_Bucket(page_id, primary.local_depth, body, primary.next_page))
        primary.next_page = page_id
        self._write_bucket(primary)
        self.bucket_pages += 1

    # ------------------------------------------------------------------ inserción
    def insert(self, value: Any, rid: RID) -> None:
        """Inserta ``(value, rid)``; en un índice único rechaza claves repetidas."""
        key = self._key_bytes(value)
        h = self.hash_bytes(key)
        ks, es = self.key_size, self.entry_size
        bucket = self._read_bucket(self._lookup(h))  # 1R directorio + 1R bucket
        if self.unique:
            for b in [bucket, *self._chain(bucket.next_page)]:
                if next(b.key_positions(key, es), None) is not None:
                    raise DuplicateKeyError(f"clave duplicada en índice único: {self.codec.decode(key)}")
        entry = self._entry(key, rid)
        self.entry_count += 1
        self._stats_dirty = True
        cap = self.bucket_capacity

        if self._count(bucket) < cap:
            bucket.body += entry
            self._write_bucket(bucket)
            return
        if bucket.next_page != NULL_PAGE:
            # Ya tiene overflow (claves con el mismo hash o profundidad máxima).
            first = self._read_bucket(bucket.next_page)
            if self._count(first) < cap:
                first.body += entry
                self._write_bucket(first)
            else:
                self._new_overflow_after(bucket, entry)
            return

        entries = bucket.entries(es) + [entry]
        page_id, local_depth = bucket.page_id, bucket.local_depth
        max_mask = (1 << self.max_depth) - 1
        while True:
            if len(entries) <= cap:
                self._write_bucket(_Bucket(page_id, local_depth, b"".join(entries)))
                return
            hashes = [self.hash_bytes(e[:ks]) for e in entries]
            if len({x & max_mask for x in hashes}) == 1 or local_depth >= self.max_depth:
                # Dividir no separa las claves: bucket de overflow.
                primary = _Bucket(page_id, local_depth, b"".join(entries[:cap]))
                self._new_overflow_after(primary, b"".join(entries[cap:]))
                return
            if local_depth == self.global_depth:
                self._double_directory()
            new_page = self.dm.allocate_page()
            self.bucket_pages += 1
            zeros = [e for e, x in zip(entries, hashes) if not (x >> local_depth) & 1]
            ones = [e for e, x in zip(entries, hashes) if (x >> local_depth) & 1]
            self._redirect(h, local_depth, new_page)
            local_depth += 1
            if (h >> (local_depth - 1)) & 1:
                self._write_bucket(_Bucket(page_id, local_depth, b"".join(zeros)))
                page_id, entries = new_page, ones
            else:
                self._write_bucket(_Bucket(new_page, local_depth, b"".join(ones)))
                entries = zeros

    # ------------------------------------------------------------------ búsqueda y borrado
    def search(self, value: Any) -> list[RID]:
        """RIDs con clave == value: 1 lectura de directorio + 1 de bucket + overflow."""
        try:
            key = self._key_bytes(value)
        except struct.error:
            return []  # p. ej. 10.5 buscado en una columna INT
        ks, es = self.key_size, self.entry_size
        out = []
        for bucket in self._chain(self._lookup(self.hash_bytes(key))):
            for pos in bucket.key_positions(key, es):
                out.append(RID(*RID_STRUCT.unpack_from(bucket.body, pos + ks)))
                if self.unique:
                    return out
        return out

    def delete(self, value: Any, rid: RID) -> bool:
        """Quita ``(value, rid)`` moviendo el último par a su lugar (1W)."""
        try:
            key = self._key_bytes(value)
        except struct.error:
            return False
        target = self._entry(key, rid)
        es = self.entry_size
        for bucket in self._chain(self._lookup(self.hash_bytes(key))):
            for pos in bucket.key_positions(key, es):
                if bucket.body[pos : pos + es] == target:
                    body = bucket.body
                    last = body[-es:]
                    # move-the-last: el último par ocupa el hueco
                    bucket.body = body[:pos] + last + body[pos + es : -es] if pos + es < len(body) else body[:pos]
                    self._write_bucket(bucket)
                    self.entry_count -= 1
                    self._stats_dirty = True
                    return True
        return False

    # ------------------------------------------------------------------ verificación
    def iter_buckets(self) -> Iterator[tuple[int, _Bucket]]:
        """Buckets primarios distintos con el primer índice de directorio que los apunta."""
        seen: set[int] = set()
        n = 1 << self.global_depth
        for p in range(self._dir_pages_needed(self.global_depth)):
            entries = self._read_dir_page(p)
            for off, bucket_id in enumerate(entries):
                i = p * self.entries_per_page + off
                if i >= n:
                    break
                if bucket_id not in seen:
                    seen.add(bucket_id)
                    yield i, self._read_bucket(bucket_id)

    def iter_all(self) -> Iterator[tuple[Any, RID]]:
        ks = self.key_size
        for _, primary in self.iter_buckets():
            for bucket in [primary, *self._chain(primary.next_page)]:
                for e in bucket.entries(self.entry_size):
                    yield self.codec.decode(e[:ks]), RID(*RID_STRUCT.unpack_from(e, ks))

    def check_invariants(self) -> dict[str, int]:
        """Verifica directorio y buckets completos (para tests)."""
        n = 1 << self.global_depth
        pointers: list[int] = []
        for p in range(self._dir_pages_needed(self.global_depth)):
            pointers.extend(self._read_dir_page(p))
        pointers = pointers[:n]
        ks = self.key_size
        total = 0
        buckets = 0
        for first_index, primary in self.iter_buckets():
            ld = primary.local_depth
            assert ld <= self.global_depth
            mask = (1 << ld) - 1
            pattern = first_index & mask
            expected = [i for i in range(n) if i & mask == pattern]
            actual = [i for i in range(n) if pointers[i] == primary.page_id]
            assert expected == actual, f"bucket {primary.page_id}: punteros del directorio inconsistentes"
            for bucket in [primary, *self._chain(primary.next_page)]:
                buckets += 1
                entries = bucket.entries(self.entry_size)
                assert len(entries) <= self.bucket_capacity
                for e in entries:
                    assert self.hash_bytes(e[:ks]) & mask == pattern, "clave en bucket equivocado"
                total += len(entries)
        assert total == self.entry_count, f"{total} entradas != {self.entry_count}"
        assert buckets == self.bucket_pages
        return {"global_depth": self.global_depth, "buckets": buckets, "entries": total}
