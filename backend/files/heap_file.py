"""Heap File con Free-List de páginas.

Archivo ``<tabla>.heap``:

* Página 0 (``FILE_META``): ``free_list_head``, ``record_size``, ``capacity`` y
  ``record_count``.
* Páginas 1..P (``HEAP_DATA``): ``[header 24][bitmap][cap * record_size]``.

La free-list es una lista simplemente enlazada (``next_page_id``) de las páginas
que tienen al menos un slot libre; su cabeza vive en la página 0. Invariante: una
página está en la lista si y solo si tiene espacio, y siempre se inserta en la
cabeza, así que sacar una página llena es O(1).

Costos (sin buffer pool):

* ``insert``: 1R + 1W si hay página en la free-list; 1W si hay que agregar una
  página al final.
* ``read(rid)``: 1R. ``delete(rid)``: 1R + 1W (si la página estaba llena vuelve a
  la cabeza de la free-list).
* ``scan``: P lecturas.

La página 0 se lee una vez al abrir y queda en memoria. Al final de cada sentencia
(``flush``) se escribe solo si cambió su estructura (cabeza de la free-list); el
contador de registros se persiste en esa escritura o al cerrar (``sync``).
"""

from __future__ import annotations

import os
from typing import Any, Callable, Iterator, Sequence

from backend.files.common import RawFilter, RecordNotFoundError
from backend.storage.disk_manager import DiskCounter, DiskManager
from backend.storage.page import (
    NULL_PAGE,
    FileKind,
    MetaCodec,
    PageFormatError,
    PageType,
    RecordPage,
    RecordPageLayout,
)
from backend.storage.record import Schema
from backend.storage.rid import RID

HEAP_META = MetaCodec(
    FileKind.HEAP,
    [("free_list_head", "i"), ("record_size", "H"), ("capacity", "H"), ("record_count", "Q")],
)


class HeapFile:
    def __init__(self, dm: DiskManager, schema: Schema, meta: dict[str, Any]) -> None:
        self.dm = dm
        self.schema = schema
        self.layout = RecordPageLayout(dm.page_size, schema.record_size)
        if int(meta["record_size"]) != schema.record_size or int(meta["capacity"]) != self.layout.capacity:
            raise PageFormatError(f"{dm.path.name}: el esquema no coincide con el archivo")
        self.free_list_head: int = int(meta["free_list_head"])
        self.record_count: int = int(meta["record_count"])
        self._dirty = False  # cambió la estructura (cabeza de la free-list)
        self._stats_dirty = False  # cambió solo el contador de registros

    # ------------------------------------------------------------------ apertura
    @classmethod
    def create(
        cls, path: str | os.PathLike[str], schema: Schema, page_size: int, counter: DiskCounter
    ) -> HeapFile:
        dm = DiskManager(path, page_size, counter, create=True)
        layout = RecordPageLayout(page_size, schema.record_size)
        meta = {
            "free_list_head": NULL_PAGE,
            "record_size": schema.record_size,
            "capacity": layout.capacity,
            "record_count": 0,
        }
        dm.write_page(dm.allocate_page(), HEAP_META.pack(page_size, 1, meta))
        return cls(dm, schema, meta)

    @classmethod
    def open(
        cls, path: str | os.PathLike[str], schema: Schema, page_size: int, counter: DiskCounter
    ) -> HeapFile:
        dm = DiskManager(path, page_size, counter)
        stored_page_size, _, meta = HEAP_META.unpack(dm.read_page(0))
        if stored_page_size != page_size:
            dm.close()
            raise PageFormatError(f"{dm.path.name}: page_size {stored_page_size} != {page_size}")
        return cls(dm, schema, meta)

    # ------------------------------------------------------------------ metadatos
    def _meta_bytes(self) -> bytes:
        return HEAP_META.pack(
            self.dm.page_size,
            self.dm.num_pages(),
            {
                "free_list_head": self.free_list_head,
                "record_size": self.schema.record_size,
                "capacity": self.layout.capacity,
                "record_count": self.record_count,
            },
        )

    def flush(self) -> None:
        """Escribe la página 0 si cambió su estructura (se llama al final de cada sentencia)."""
        if self._dirty:
            self.dm.write_page(0, self._meta_bytes())
            self._dirty = self._stats_dirty = False

    def sync(self) -> None:
        """Persiste también las estadísticas (al cerrar o antes de inspeccionar la página 0)."""
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
        """Páginas de datos (sin la página 0)."""
        return self.dm.num_pages() - 1

    @property
    def capacity(self) -> int:
        return self.layout.capacity

    # ------------------------------------------------------------------ operaciones
    def _load(self, page_id: int) -> RecordPage:
        return RecordPage.from_bytes(self.layout, self.dm.read_page(page_id), PageType.HEAP_DATA)

    def insert(self, values: Sequence[Any]) -> RID:
        """Inserta valores ya validados (``schema.coerce``) y devuelve su RID."""
        return self.insert_raw(self.schema.encode(values))

    def insert_raw(self, record: bytes) -> RID:
        while self.free_list_head != NULL_PAGE:
            page_id = self.free_list_head
            page = self._load(page_id)  # 1R
            if not page.has_space():
                # Cabeza obsoleta (p. ej. metadatos no persistidos tras una caída): se descarta.
                self.free_list_head = page.header.next_page_id
                self._dirty = True
                continue
            slot = page.insert(record)
            if not page.has_space():
                self.free_list_head = page.header.next_page_id
                page.header.next_page_id = NULL_PAGE
                self._dirty = True
            self.dm.write_page(page_id, page.to_bytes())  # 1W
            break
        else:
            page_id = self.dm.allocate_page()
            page = RecordPage.new(self.layout, page_id, PageType.HEAP_DATA)
            slot = page.insert(record)
            if page.has_space():
                page.header.next_page_id = self.free_list_head
                self.free_list_head = page_id
            self._dirty = True
            self.dm.write_page(page_id, page.to_bytes())  # 1W
        self.record_count += 1
        self._stats_dirty = True
        return RID(page_id, slot)

    def read(self, rid: RID) -> tuple[Any, ...]:
        """Trae el registro apuntado por ``rid`` (1R)."""
        if not 1 <= rid.page_id < self.dm.num_pages():
            raise RecordNotFoundError(f"RID {rid} fuera del archivo")
        page = self._load(rid.page_id)
        if rid.slot >= self.layout.capacity or not page.is_used(rid.slot):
            raise RecordNotFoundError(f"RID {rid} no apunta a un registro activo")
        return self.schema.decode(page.buf, self.layout.slot_offset(rid.slot))

    def _delete_slots(self, page: RecordPage, slots: Sequence[int]) -> None:
        was_full = not page.has_space()
        for slot in slots:
            page.delete(slot)
        if was_full and slots:
            # La página vuelve a tener espacio: entra por la cabeza de la free-list.
            page.header.next_page_id = self.free_list_head
            self.free_list_head = page.page_id
            self._dirty = True
        self.record_count -= len(slots)
        self._stats_dirty = True

    def delete(self, rid: RID) -> tuple[Any, ...]:
        """Borrado lógico de un registro (1R + 1W). Devuelve sus valores."""
        return self.delete_many([rid])[0][1]

    def delete_many(self, rids: Sequence[RID]) -> list[tuple[RID, tuple[Any, ...]]]:
        """Borra varios RIDs agrupándolos por página: 1R + 1W por página distinta."""
        by_page: dict[int, list[int]] = {}
        for rid in rids:
            by_page.setdefault(rid.page_id, []).append(rid.slot)
        deleted: list[tuple[RID, tuple[Any, ...]]] = []
        for page_id in sorted(by_page):
            if not 1 <= page_id < self.dm.num_pages():
                raise RecordNotFoundError(f"página {page_id} fuera del archivo")
            page = self._load(page_id)
            slots = sorted(set(by_page[page_id]))
            for slot in slots:
                if slot >= self.layout.capacity or not page.is_used(slot):
                    raise RecordNotFoundError(f"RID ({page_id},{slot}) no apunta a un registro activo")
                deleted.append((RID(page_id, slot), self.schema.decode(page.buf, self.layout.slot_offset(slot))))
            self._delete_slots(page, slots)
            self.dm.write_page(page_id, page.to_bytes())
        return deleted

    def scan(self, raw_filter: RawFilter | None = None) -> Iterator[tuple[RID, tuple[Any, ...]]]:
        """Full table scan: lee las páginas 1..P (P lecturas) en orden físico."""
        decode = self.schema.decode
        base = self.layout.slots_offset
        rs = self.layout.record_size
        for page_id in range(1, self.dm.num_pages()):
            page = self._load(page_id)
            buf = page.buf
            for slot in page.used_slots():
                off = base + slot * rs
                if raw_filter is None or raw_filter(buf, off):
                    yield RID(page_id, slot), decode(buf, off)

    def delete_where(self, raw_filter: RawFilter | None) -> Iterator[tuple[RID, tuple[Any, ...]]]:
        """Borra durante un full scan: P lecturas + 1 escritura por página modificada."""
        decode = self.schema.decode
        base = self.layout.slots_offset
        rs = self.layout.record_size
        for page_id in range(1, self.dm.num_pages()):
            page = self._load(page_id)
            buf = page.buf
            victims = []
            for slot in page.used_slots():
                off = base + slot * rs
                if raw_filter is None or raw_filter(buf, off):
                    victims.append((slot, decode(buf, off)))
            if not victims:
                continue
            self._delete_slots(page, [slot for slot, _ in victims])
            self.dm.write_page(page_id, page.to_bytes())
            for slot, values in victims:
                yield RID(page_id, slot), values

    # ------------------------------------------------------------------ utilidades
    def free_list(self, limit: int | None = None) -> list[int]:
        """Recorre la free-list (cuesta 1R por página; solo para tests e inspección)."""
        pages = []
        page_id = self.free_list_head
        while page_id != NULL_PAGE and (limit is None or len(pages) < limit):
            pages.append(page_id)
            page_id = self._load(page_id).header.next_page_id
        return pages

    def for_each_page(self, fn: Callable[[RecordPage], None]) -> None:
        for page_id in range(1, self.dm.num_pages()):
            fn(self._load(page_id))
