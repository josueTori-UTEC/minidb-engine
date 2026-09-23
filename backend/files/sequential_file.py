"""Sequential File: área principal ordenada por la PK + área de overflow encadenada.

Archivos:

* ``<tabla>.seq`` (área principal). Página 0 = metadatos. Páginas 1..M
  (``SEQ_MAIN``) ordenadas por rango de clave: todas las claves de la página i son
  menores que la clave frontera de la página i+1. Eso permite **búsqueda binaria
  sobre páginas** (~log2 M lecturas). ``next_page_id``/``prev_page_id`` enlazan las
  páginas vecinas y ``aux_page_id`` apunta a la cabeza de su cadena de overflow.
* ``<tabla>.ovf`` (área de overflow). Páginas ``SEQ_OVERFLOW`` encadenadas con
  ``next_page_id``; ``aux_page_id`` guarda la página principal dueña de la cadena.

Clave frontera: tras ``reorganize`` los registros de cada página principal quedan
ordenados en los slots 0..k-1, así que el slot 0 guarda la menor clave de la
página. En el área principal los slots borrados NO se reutilizan (quedan como
lápidas hasta la siguiente reorganización), de modo que el slot 0 nunca se
sobrescribe y sirve como frontera estable para la búsqueda binaria. Los slots
nuevos se toman de la marca de agua (``free_space_offset``) que deja el fill
factor.

Inserción: búsqueda binaria de la página destino; si le queda un slot virgen se
inserta ahí sin mover a nadie (1W). Si no, va a la cabeza de su cadena de
overflow (1R + 1W) o a una página de overflow nueva que pasa a ser la cabeza
(1W + 1W de la página principal). Si la clave es mayor que todas las de la
última página y esta no tiene overflow, se agrega una página principal nueva
(inserciones en orden creciente no generan overflow).

``reorganize`` recorre las páginas principales en orden, fusiona cada una con su
cadena de overflow (ordenando en memoria, o con ordenamiento externo si la
cadena es muy larga), reescribe un archivo principal nuevo con el fill factor
configurado (0.70-0.80), lo reemplaza con ``os.replace`` y vacía el overflow. Los
RIDs cambian: la capa de tabla reconstruye los índices después.

Un RID con ``page_id`` negativo apunta al overflow (``RID(-k, s)`` = página k del
``.ovf``).
"""

from __future__ import annotations

import heapq
import os
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Sequence

from backend.files.common import DuplicateKeyError, RawFilter, RecordNotFoundError
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
from backend.storage.record import ColumnType, Schema, encode_str
from backend.storage.rid import RID

SEQ_META = MetaCodec(
    FileKind.SEQ_MAIN,
    [
        ("record_size", "H"),
        ("capacity", "H"),
        ("key_column", "H"),
        ("fill_factor", "H"),  # milésimas
        ("record_count", "Q"),
        ("reorganizations", "I"),
    ],
)
OVF_META = MetaCodec(FileKind.SEQ_OVERFLOW, [("record_size", "H"), ("capacity", "H"), ("main_file_pages", "I")])
RUN_META = MetaCodec(FileKind.SORT_RUN, [("record_size", "H")])

DEFAULT_FILL_FACTOR = 0.75
DEFAULT_REORG_RATIO = 0.10
# Páginas (principal + overflow) que se ordenan en memoria al leer una página
# lógica; por encima se usa ordenamiento externo (reorganize) o se entrega sin
# ordenar (scan) para no cargar una cadena gigantesca en memoria.
IN_MEMORY_SORT_PAGES = 64


@dataclass
class ReorganizeStats:
    main_pages_before: int
    overflow_pages_before: int
    main_pages_after: int
    records: int
    external_sorts: int


class SequentialFile:
    def __init__(
        self,
        main_path: Path,
        overflow_path: Path,
        schema: Schema,
        page_size: int,
        counter: DiskCounter,
        *,
        fill_factor: float,
        reorg_ratio: float,
        auto_reorganize: bool,
    ) -> None:
        if schema.pk_index is None:
            raise PageFormatError("un Sequential File necesita una PRIMARY KEY")
        if not 0.5 <= fill_factor <= 1.0:
            raise ValueError("fill_factor debe estar entre 0.5 y 1.0 (recomendado 0.70-0.80)")
        self.main_path = Path(main_path)
        self.overflow_path = Path(overflow_path)
        self.schema = schema
        self.page_size = page_size
        self.counter = counter
        self.fill_factor = fill_factor
        self.reorg_ratio = reorg_ratio
        self.auto_reorganize = auto_reorganize
        self.layout = RecordPageLayout(page_size, schema.record_size)
        self.key_index = schema.pk_index
        key_col = schema.columns[self.key_index]
        self._key_struct = struct.Struct("<" + key_col.fmt)
        self._key_offset = schema.offsets[self.key_index]
        self._key_is_char = key_col.type == ColumnType.CHAR
        self._key_size = self._key_struct.size
        self.main: DiskManager
        self.ovf: DiskManager
        self.record_count = 0
        self.reorganizations = 0
        self._dirty = False
        self._stats_dirty = False
        self._ovf_meta_pages = 1

    # ------------------------------------------------------------------ apertura
    @classmethod
    def create(
        cls,
        main_path: str | os.PathLike[str],
        overflow_path: str | os.PathLike[str],
        schema: Schema,
        page_size: int,
        counter: DiskCounter,
        *,
        fill_factor: float = DEFAULT_FILL_FACTOR,
        reorg_ratio: float = DEFAULT_REORG_RATIO,
        auto_reorganize: bool = True,
    ) -> SequentialFile:
        sf = cls(
            Path(main_path),
            Path(overflow_path),
            schema,
            page_size,
            counter,
            fill_factor=fill_factor,
            reorg_ratio=reorg_ratio,
            auto_reorganize=auto_reorganize,
        )
        sf.main = DiskManager(sf.main_path, page_size, counter, create=True)
        sf.ovf = DiskManager(sf.overflow_path, page_size, counter, create=True)
        sf.main.allocate_page()
        sf.ovf.allocate_page()
        sf.main.write_page(0, sf._main_meta_bytes())
        sf.ovf.write_page(0, sf._ovf_meta_bytes())
        return sf

    @classmethod
    def open(
        cls,
        main_path: str | os.PathLike[str],
        overflow_path: str | os.PathLike[str],
        schema: Schema,
        page_size: int,
        counter: DiskCounter,
        *,
        reorg_ratio: float = DEFAULT_REORG_RATIO,
        auto_reorganize: bool = True,
    ) -> SequentialFile:
        main = DiskManager(main_path, page_size, counter)
        stored_ps, _, meta = SEQ_META.unpack(main.read_page(0))
        ovf = DiskManager(overflow_path, page_size, counter)
        OVF_META.unpack(ovf.read_page(0))
        if stored_ps != page_size or int(meta["record_size"]) != schema.record_size:
            main.close()
            ovf.close()
            raise PageFormatError(f"{Path(main_path).name}: el esquema no coincide con el archivo")
        sf = cls(
            Path(main_path),
            Path(overflow_path),
            schema,
            page_size,
            counter,
            fill_factor=int(meta["fill_factor"]) / 1000,
            reorg_ratio=reorg_ratio,
            auto_reorganize=auto_reorganize,
        )
        sf.main, sf.ovf = main, ovf
        sf._ovf_meta_pages = ovf.num_pages()
        sf.record_count = int(meta["record_count"])
        sf.reorganizations = int(meta["reorganizations"])
        return sf

    # ------------------------------------------------------------------ metadatos
    def _main_meta_bytes(self) -> bytes:
        return SEQ_META.pack(
            self.page_size,
            self.main.num_pages(),
            {
                "record_size": self.schema.record_size,
                "capacity": self.layout.capacity,
                "key_column": self.key_index,
                "fill_factor": round(self.fill_factor * 1000),
                "record_count": self.record_count,
                "reorganizations": self.reorganizations,
            },
        )

    def _ovf_meta_bytes(self) -> bytes:
        return OVF_META.pack(
            self.page_size,
            self.ovf.num_pages(),
            {
                "record_size": self.schema.record_size,
                "capacity": self.layout.capacity,
                "main_file_pages": self.main.num_pages(),
            },
        )

    def flush(self) -> None:
        """Escribe la página 0 del área principal si cambió su estructura."""
        if self._dirty:
            self.main.write_page(0, self._main_meta_bytes())
            self._dirty = self._stats_dirty = False

    def sync(self) -> None:
        """Persiste también estadísticas y el nº de páginas del overflow (al cerrar)."""
        if self._dirty or self._stats_dirty:
            self.main.write_page(0, self._main_meta_bytes())
            self._dirty = self._stats_dirty = False
        if self._ovf_meta_pages != self.ovf.num_pages():
            self.ovf.write_page(0, self._ovf_meta_bytes())
            self._ovf_meta_pages = self.ovf.num_pages()
        self.main.flush()
        self.ovf.flush()

    def close(self) -> None:
        if not self.main.closed:
            self.sync()
            self.main.close()
            self.ovf.close()

    # ------------------------------------------------------------------ estadísticas
    @property
    def main_pages(self) -> int:
        return self.main.num_pages() - 1

    @property
    def overflow_pages(self) -> int:
        return self.ovf.num_pages() - 1

    @property
    def page_count(self) -> int:
        return self.main_pages + self.overflow_pages

    @property
    def capacity(self) -> int:
        return self.layout.capacity

    @property
    def records_per_page_after_reorg(self) -> int:
        return max(1, int(self.layout.capacity * self.fill_factor))

    def reorganization_threshold(self) -> int:
        return max(1, int(self.reorg_ratio * self.main_pages))

    def needs_reorganization(self) -> bool:
        return self.auto_reorganize and self.overflow_pages > self.reorganization_threshold()

    # ------------------------------------------------------------------ claves
    def key_of(self, buf: bytes | bytearray | memoryview, offset: int = 0) -> Any:
        """Clave comparable de un registro crudo (CHAR se compara como bytes rellenados)."""
        return self._key_struct.unpack_from(buf, offset + self._key_offset)[0]

    def comparable(self, value: Any) -> Any:
        """Lleva un literal (ya normalizado a la columna) al dominio de ``key_of``."""
        if self._key_is_char:
            return encode_str(value, self._key_size).ljust(self._key_size, b"\x00")
        return value

    # ------------------------------------------------------------------ páginas
    def _load_main(self, page_id: int) -> RecordPage:
        return RecordPage.from_bytes(self.layout, self.main.read_page(page_id), PageType.SEQ_MAIN)

    def _load_ovf(self, page_id: int) -> RecordPage:
        return RecordPage.from_bytes(self.layout, self.ovf.read_page(page_id), PageType.SEQ_OVERFLOW)

    def _fence(self, page: RecordPage) -> Any:
        return self.key_of(page.buf, self.layout.slots_offset)

    def _max_key_ever(self, page: RecordPage) -> Any:
        """Máxima clave almacenada alguna vez en los slots usados (incluye lápidas)."""
        base, rs = self.layout.slots_offset, self.layout.record_size
        return max(self.key_of(page.buf, base + s * rs) for s in range(page.high_water_mark()))

    def _locate(self, key: Any) -> RecordPage | None:
        """Búsqueda binaria de la página principal cuyo rango contiene ``key``.

        Devuelve la última página cuya clave frontera es <= key (o la página 1).
        Cuesta ~ceil(log2 M) lecturas (+1 si la página 1 no fue sondeada).
        """
        lo, hi = 1, self.main_pages
        if hi < 1:
            return None
        lo_page: RecordPage | None = None
        while lo < hi:
            mid = (lo + hi + 1) // 2
            page = self._load_main(mid)
            if self._fence(page) <= key:
                lo, lo_page = mid, page
            else:
                hi = mid - 1
        return lo_page if lo_page is not None else self._load_main(lo)

    def _chain(self, head: int) -> Iterator[RecordPage]:
        page_id = head
        while page_id != NULL_PAGE:
            page = self._load_ovf(page_id)
            yield page
            page_id = page.header.next_page_id

    def _find_in_page(self, page: RecordPage, key: Any) -> int:
        base, rs = self.layout.slots_offset, self.layout.record_size
        for slot in page.used_slots():
            if self.key_of(page.buf, base + slot * rs) == key:
                return slot
        return -1

    # ------------------------------------------------------------------ inserción
    def insert(self, values: Sequence[Any], *, check_unique: bool = True) -> RID:
        """Inserta un registro ya validado. ``check_unique`` busca la PK en la página
        destino y su cadena de overflow antes de insertar."""
        record = self.schema.encode(values)
        key = self.key_of(record)
        if self.main_pages == 0:
            page_id = self.main.allocate_page()
            page = RecordPage.new(self.layout, page_id, PageType.SEQ_MAIN)
            slot = page.insert(record)
            self.main.write_page(page_id, page.to_bytes())
            self._after_insert(structural=True)
            return RID(page_id, slot)

        page = self._locate(key)
        assert page is not None
        chain_head: RecordPage | None = None
        if check_unique:
            if self._find_in_page(page, key) >= 0:
                raise DuplicateKeyError(f"clave primaria duplicada: {self._display_key(key)}")
            for i, ovf_page in enumerate(self._chain(page.header.aux_page_id)):
                if i == 0:
                    chain_head = ovf_page
                if self._find_in_page(ovf_page, key) >= 0:
                    raise DuplicateKeyError(f"clave primaria duplicada: {self._display_key(key)}")

        if page.has_space():
            slot = page.insert(record)
            self.main.write_page(page.page_id, page.to_bytes())
            self._after_insert()
            return RID(page.page_id, slot)

        if (
            page.page_id == self.main_pages
            and page.header.aux_page_id == NULL_PAGE
            and key > self._max_key_ever(page)
        ):
            # Clave mayor que todo el archivo: nueva página principal al final.
            new_id = self.main.allocate_page()
            new_page = RecordPage.new(self.layout, new_id, PageType.SEQ_MAIN)
            new_page.header.prev_page_id = page.page_id
            slot = new_page.insert(record)
            page.header.next_page_id = new_id
            self.main.write_page(page.page_id, page.to_bytes())
            self.main.write_page(new_id, new_page.to_bytes())
            self._after_insert(structural=True)
            return RID(new_id, slot)

        head_id = page.header.aux_page_id
        if head_id != NULL_PAGE:
            head = chain_head if chain_head is not None else self._load_ovf(head_id)
            if head.has_space():
                slot = head.insert(record)
                self.ovf.write_page(head_id, head.to_bytes())
                self._after_insert()
                return RID(-head_id, slot)
        new_id = self.ovf.allocate_page()
        ovf_page = RecordPage.new(self.layout, new_id, PageType.SEQ_OVERFLOW)
        ovf_page.header.next_page_id = head_id
        ovf_page.header.aux_page_id = page.page_id
        slot = ovf_page.insert(record)
        self.ovf.write_page(new_id, ovf_page.to_bytes())
        page.header.aux_page_id = new_id
        self.main.write_page(page.page_id, page.to_bytes())
        self._after_insert()
        return RID(-new_id, slot)

    def _after_insert(self, structural: bool = False) -> None:
        self.record_count += 1
        self._stats_dirty = True
        if structural:
            self._dirty = True

    def _display_key(self, key: Any) -> Any:
        return key.rstrip(b"\x00").decode("utf-8", errors="ignore") if self._key_is_char else key

    # ------------------------------------------------------------------ lectura
    def read(self, rid: RID) -> tuple[Any, ...]:
        """Trae el registro apuntado por ``rid`` (1R)."""
        page = self._page_for_rid(rid)
        if rid.slot >= self.layout.capacity or not page.is_used(rid.slot):
            raise RecordNotFoundError(f"RID {rid} no apunta a un registro activo")
        return self.schema.decode(page.buf, self.layout.slot_offset(rid.slot))

    def _page_for_rid(self, rid: RID) -> RecordPage:
        if rid.page_id > 0 and rid.page_id <= self.main_pages:
            return self._load_main(rid.page_id)
        if rid.page_id < 0 and -rid.page_id <= self.overflow_pages:
            return self._load_ovf(-rid.page_id)
        raise RecordNotFoundError(f"RID {rid} fuera del archivo")

    def find(self, value: Any) -> Iterator[tuple[RID, tuple[Any, ...]]]:
        """Búsqueda binaria por la PK: log2(M) lecturas + la cadena de overflow si hace falta."""
        key = self.comparable(value)
        page = self._locate(key)
        if page is None:
            return
        slot = self._find_in_page(page, key)
        if slot >= 0:
            yield RID(page.page_id, slot), self.schema.decode(page.buf, self.layout.slot_offset(slot))
            return
        for ovf_page in self._chain(page.header.aux_page_id):
            slot = self._find_in_page(ovf_page, key)
            if slot >= 0:
                yield RID(-ovf_page.page_id, slot), self.schema.decode(
                    ovf_page.buf, self.layout.slot_offset(slot)
                )
                return

    def _group_records(
        self, page: RecordPage, raw_filter: RawFilter | None
    ) -> tuple[list[tuple[Any, RID, tuple[Any, ...]]], Any, bool]:
        """Registros (clave, rid, valores) de una página lógica (principal + overflow).

        Devuelve también la mayor clave almacenada y si el grupo pudo ordenarse.
        """
        base, rs = self.layout.slots_offset, self.layout.record_size
        decode = self.schema.decode
        out: list[tuple[Any, RID, tuple[Any, ...]]] = []
        max_key = self._max_key_ever(page) if page.high_water_mark() else None
        pages = 1
        for slot in page.used_slots():
            off = base + slot * rs
            if raw_filter is None or raw_filter(page.buf, off):
                out.append((self.key_of(page.buf, off), RID(page.page_id, slot), decode(page.buf, off)))
        for ovf_page in self._chain(page.header.aux_page_id):
            pages += 1
            for slot in ovf_page.used_slots():
                off = base + slot * rs
                k = self.key_of(ovf_page.buf, off)
                if max_key is None or k > max_key:
                    max_key = k
                if raw_filter is None or raw_filter(ovf_page.buf, off):
                    out.append((k, RID(-ovf_page.page_id, slot), decode(ovf_page.buf, off)))
        sortable = pages <= IN_MEMORY_SORT_PAGES
        if sortable:
            out.sort(key=lambda item: item[0])
        return out, max_key, sortable

    def scan(self, raw_filter: RawFilter | None = None) -> Iterator[tuple[RID, tuple[Any, ...]]]:
        """Recorre todas las páginas principales y sus cadenas (M + O lecturas), en orden de PK."""
        for page_id in range(1, self.main_pages + 1):
            group, _, _ = self._group_records(self._load_main(page_id), raw_filter)
            for _, rid, values in group:
                yield rid, values

    def range_scan(
        self,
        low: Any = None,
        high: Any = None,
        *,
        low_inclusive: bool = True,
        high_inclusive: bool = True,
        raw_filter: RawFilter | None = None,
    ) -> Iterator[tuple[RID, tuple[Any, ...]]]:
        """Rango sobre la PK: búsqueda binaria de ``low`` y recorrido secuencial de páginas."""
        lo = None if low is None else self.comparable(low)
        hi = None if high is None else self.comparable(high)
        if self.main_pages == 0:
            return
        page = self._locate(lo) if lo is not None else self._load_main(1)
        assert page is not None
        while True:
            group, max_key, _ = self._group_records(page, raw_filter)
            for k, rid, values in group:
                if lo is not None and (k < lo or (k == lo and not low_inclusive)):
                    continue
                if hi is not None and (k > hi or (k == hi and not high_inclusive)):
                    continue
                yield rid, values
            # Si esta página ya guarda una clave > high, las siguientes también superan high.
            if hi is not None and max_key is not None and max_key > hi:
                return
            nxt = page.page_id + 1
            if nxt > self.main_pages:
                return
            page = self._load_main(nxt)
            if hi is not None and self._fence(page) > hi:
                return

    # ------------------------------------------------------------------ borrado
    def delete(self, rid: RID) -> tuple[Any, ...]:
        return self.delete_many([rid])[0][1]

    def delete_many(self, rids: Sequence[RID]) -> list[tuple[RID, tuple[Any, ...]]]:
        """Borrado lógico agrupado por página (1R + 1W por página distinta)."""
        by_page: dict[int, list[int]] = {}
        for rid in rids:
            by_page.setdefault(rid.page_id, []).append(rid.slot)
        deleted: list[tuple[RID, tuple[Any, ...]]] = []
        for page_id in sorted(by_page):
            page = self._page_for_rid(RID(page_id, 0))
            for slot in sorted(set(by_page[page_id])):
                if slot >= self.layout.capacity or not page.is_used(slot):
                    raise RecordNotFoundError(f"RID ({page_id},{slot}) no apunta a un registro activo")
                deleted.append((RID(page_id, slot), self.schema.decode(page.buf, self.layout.slot_offset(slot))))
                page.delete(slot)
            self._write_page_of_rid(page_id, page)
        self.record_count -= len(deleted)
        self._stats_dirty = True
        return deleted

    def _write_page_of_rid(self, page_id: int, page: RecordPage) -> None:
        if page_id > 0:
            self.main.write_page(page_id, page.to_bytes())
        else:
            self.ovf.write_page(-page_id, page.to_bytes())

    def delete_where(self, raw_filter: RawFilter | None) -> Iterator[tuple[RID, tuple[Any, ...]]]:
        """Borra durante un recorrido completo (principal + overflow)."""
        base, rs = self.layout.slots_offset, self.layout.record_size
        decode = self.schema.decode
        for page_id in range(1, self.main_pages + 1):
            page = self._load_main(page_id)
            groups = [(page, page_id)]
            groups.extend((ovf_page, -ovf_page.page_id) for ovf_page in self._chain(page.header.aux_page_id))
            for current, rid_page in groups:
                victims = []
                for slot in current.used_slots():
                    off = base + slot * rs
                    if raw_filter is None or raw_filter(current.buf, off):
                        victims.append((slot, decode(current.buf, off)))
                if not victims:
                    continue
                for slot, _ in victims:
                    current.delete(slot)
                self._write_page_of_rid(rid_page, current)
                self.record_count -= len(victims)
                self._stats_dirty = True
                for slot, values in victims:
                    yield RID(rid_page, slot), values

    # ------------------------------------------------------------------ reorganización
    def reorganize(self) -> ReorganizeStats:
        """Reescribe el área principal ordenada con el fill factor y vacía el overflow."""
        before_main, before_ovf = self.main_pages, self.overflow_pages
        tmp_path = self.main_path.with_name(self.main_path.name + ".tmp")
        if tmp_path.exists():
            tmp_path.unlink()
        writer = _MainAreaWriter(tmp_path, self.layout, self.counter, self.records_per_page_after_reorg)
        external_sorts = 0
        total = 0
        for page_id in range(1, before_main + 1):
            page = self._load_main(page_id)
            # La cadena se lee de forma perezosa: si supera IN_MEMORY_SORT_PAGES
            # se pasa a ordenamiento externo con lo que falta por leer.
            group_pages: list[RecordPage] = [page]
            chain_iter = self._chain(page.header.aux_page_id)
            overflowed = False
            for ovf_page in chain_iter:
                group_pages.append(ovf_page)
                if len(group_pages) > IN_MEMORY_SORT_PAGES:
                    overflowed = True
                    break
            if not overflowed:
                records = [rec for p in group_pages for _, rec in p.records()]
                records.sort(key=self.key_of)
                for rec in records:
                    writer.append(rec)
                total += len(records)
            else:
                external_sorts += 1
                for rec in self._external_sort(group_pages, chain_iter):
                    writer.append(rec)
                    total += 1
        new_main_pages = writer.finish(
            lambda num_pages: SEQ_META.pack(
                self.page_size,
                num_pages,
                {
                    "record_size": self.schema.record_size,
                    "capacity": self.layout.capacity,
                    "key_column": self.key_index,
                    "fill_factor": round(self.fill_factor * 1000),
                    "record_count": total,
                    "reorganizations": self.reorganizations + 1,
                },
            )
        )
        self.main.close()
        os.replace(tmp_path, self.main_path)
        self.main = DiskManager(self.main_path, self.page_size, self.counter)
        self.ovf.truncate(1)
        self.ovf.write_page(0, self._ovf_meta_bytes())
        self._ovf_meta_pages = 1
        self.record_count = total
        self.reorganizations += 1
        self._dirty = self._stats_dirty = False
        return ReorganizeStats(before_main, before_ovf, new_main_pages, total, external_sorts)

    def _external_sort(self, first_pages: list[RecordPage], rest: Iterator[RecordPage]) -> Iterator[bytes]:
        """Ordenamiento externo de una cadena larga: runs de IN_MEMORY_SORT_PAGES
        páginas ordenadas en memoria, escritas a un archivo temporal y fusionadas
        con un heap (1 página en memoria por run)."""
        run_path = self.main_path.with_name(self.main_path.name + ".run")
        if run_path.exists():
            run_path.unlink()
        run_layout = self.layout
        runs: list[tuple[int, int]] = []  # (primera página, nº de páginas)
        with DiskManager(run_path, self.page_size, self.counter, create=True) as run_dm:
            run_dm.allocate_page()
            run_dm.write_page(0, RUN_META.pack(self.page_size, 1, {"record_size": self.schema.record_size}))

            def write_run(batch: list[RecordPage]) -> None:
                records = [rec for p in batch for _, rec in p.records()]
                records.sort(key=self.key_of)
                start = run_dm.num_pages()
                count = 0
                for i in range(0, len(records), run_layout.capacity):
                    pid = run_dm.allocate_page()
                    rp = RecordPage.new(run_layout, pid, PageType.SORT_RUN)
                    for rec in records[i : i + run_layout.capacity]:
                        rp.insert(rec)
                    run_dm.write_page(pid, rp.to_bytes())
                    count += 1
                if count:
                    runs.append((start, count))

            batch = list(first_pages)
            for page in rest:
                if len(batch) >= IN_MEMORY_SORT_PAGES:
                    write_run(batch)
                    batch = []
                batch.append(page)
            if batch:
                write_run(batch)

            def run_iter(start: int, count: int) -> Iterator[bytes]:
                for pid in range(start, start + count):
                    rp = RecordPage.from_bytes(run_layout, run_dm.read_page(pid), PageType.SORT_RUN)
                    for _, rec in rp.records():
                        yield rec

            yield from heapq.merge(*(run_iter(s, c) for s, c in runs), key=self.key_of)
        run_path.unlink()

    # ------------------------------------------------------------------ inspección
    def main_page_info(self, page_id: int) -> dict[str, Any]:
        page = self._load_main(page_id)
        return {"fence": self._display_key(self._fence(page)), "overflow_head": page.header.aux_page_id}


class _MainAreaWriter:
    """Escribe páginas SEQ_MAIN nuevas en orden, cada una con ``per_page`` registros.

    Mantiene la página en curso en memoria hasta saber si habrá una siguiente, así
    cada página se escribe exactamente una vez con sus punteros next/prev finales.
    """

    def __init__(self, path: Path, layout: RecordPageLayout, counter: DiskCounter, per_page: int) -> None:
        self.dm = DiskManager(path, layout.page_size, counter, create=True)
        self.dm.allocate_page()  # página 0: se escribe al final
        self.layout = layout
        self.per_page = per_page
        self.current: RecordPage | None = None

    def append(self, record: bytes) -> None:
        if self.current is None or self.current.header.record_count >= self.per_page:
            new_id = self.dm.allocate_page()
            new_page = RecordPage.new(self.layout, new_id, PageType.SEQ_MAIN)
            if self.current is not None:
                self.current.header.next_page_id = new_id
                new_page.header.prev_page_id = self.current.page_id
                self.dm.write_page(self.current.page_id, self.current.to_bytes())
            self.current = new_page
        self.current.insert(record)

    def finish(self, meta_factory: Any) -> int:
        if self.current is not None:
            self.dm.write_page(self.current.page_id, self.current.to_bytes())
        num_pages = self.dm.num_pages()
        self.dm.write_page(0, meta_factory(num_pages))
        self.dm.close()
        return num_pages - 1
