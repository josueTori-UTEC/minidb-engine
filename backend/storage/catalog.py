"""Catálogo del sistema persistido en ``<MINIDB_DATA_DIR>/catalog.bin``.

Guarda las tablas (columnas, tipos, PK, organización, page_size y archivos) y sus
índices (nombre, columna, tipo y archivo) en formato binario paginado, sin JSON
ni pickle:

* Página 0 (``FILE_META``): ``payload_length``, ``data_pages`` y ``generation``.
* Páginas 1..k (``CATALOG``): el payload serializado con ``struct``, repartido en
  trozos de ``page_size - 24`` bytes; ``free_space_offset`` = bytes útiles de la
  página y ``next_page_id`` = siguiente trozo.

Cada cambio de DDL reescribe el catálogo completo (es pequeño) y esas escrituras
se cuentan en el ``DiskCounter`` de la sentencia.
"""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from backend.storage.disk_manager import DiskCounter, DiskManager
from backend.storage.page import (
    NULL_PAGE,
    PAGE_HEADER_SIZE,
    FileKind,
    MetaCodec,
    PageHeader,
    PageType,
    expect_page_type,
)
from backend.storage.record import CODE_TYPES, TYPE_CODES, Column, Schema

CATALOG_FILE = "catalog.bin"
CATALOG_PAGE_SIZE = 4096

_META = MetaCodec(
    FileKind.CATALOG,
    [("payload_length", "I"), ("data_pages", "I"), ("generation", "I")],
)


class CatalogError(Exception):
    """Objeto inexistente o duplicado en el catálogo."""


class Organization(str, Enum):
    HEAP = "HEAP"
    SEQUENTIAL = "SEQUENTIAL"


class IndexKind(str, Enum):
    BTREE = "BTREE"
    HASH = "HASH"


_ORG_CODES = {Organization.HEAP: 1, Organization.SEQUENTIAL: 2}
_INDEX_CODES = {IndexKind.BTREE: 1, IndexKind.HASH: 2}


@dataclass
class IndexInfo:
    name: str
    table: str
    column: str
    kind: IndexKind
    file: str


@dataclass
class TableInfo:
    name: str
    columns: list[Column]
    organization: Organization
    page_size: int
    files: list[str]
    indexes: list[IndexInfo] = field(default_factory=list)
    # Solo para SEQUENTIAL (se guardan en milésimas).
    fill_factor: float = 0.75
    reorg_ratio: float = 0.10
    auto_reorganize: bool = True

    @property
    def schema(self) -> Schema:
        return Schema(self.columns)

    def index_named(self, name: str) -> IndexInfo | None:
        return next((i for i in self.indexes if i.name == name), None)


# ---------------------------------------------------------------------- serialización
class _Writer:
    def __init__(self) -> None:
        self.parts: list[bytes] = []

    def u8(self, v: int) -> None:
        self.parts.append(struct.pack("<B", v))

    def u16(self, v: int) -> None:
        self.parts.append(struct.pack("<H", v))

    def u32(self, v: int) -> None:
        self.parts.append(struct.pack("<I", v))

    def text(self, s: str) -> None:
        raw = s.encode("utf-8")
        self.u16(len(raw))
        self.parts.append(raw)

    def getvalue(self) -> bytes:
        return b"".join(self.parts)


class _Reader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def _take(self, fmt: str) -> int:
        (value,) = struct.unpack_from(fmt, self.data, self.pos)
        self.pos += struct.calcsize(fmt)
        return value

    def u8(self) -> int:
        return self._take("<B")

    def u16(self) -> int:
        return self._take("<H")

    def u32(self) -> int:
        return self._take("<I")

    def text(self) -> str:
        n = self.u16()
        raw = self.data[self.pos : self.pos + n]
        self.pos += n
        return raw.decode("utf-8")


def serialize_tables(tables: list[TableInfo]) -> bytes:
    w = _Writer()
    w.u16(len(tables))
    for t in tables:
        w.text(t.name)
        w.u8(_ORG_CODES[t.organization])
        w.u32(t.page_size)
        w.u16(round(t.fill_factor * 1000))
        w.u16(round(t.reorg_ratio * 1000))
        w.u8(1 if t.auto_reorganize else 0)
        w.u8(len(t.columns))
        for c in t.columns:
            w.text(c.name)
            w.u8(TYPE_CODES[c.type])
            w.u16(c.size or 0)
            w.u8(1 if c.primary_key else 0)
        w.u8(len(t.files))
        for f in t.files:
            w.text(f)
        w.u8(len(t.indexes))
        for idx in t.indexes:
            w.text(idx.name)
            w.text(idx.column)
            w.u8(_INDEX_CODES[idx.kind])
            w.text(idx.file)
    return w.getvalue()


def deserialize_tables(data: bytes) -> list[TableInfo]:
    r = _Reader(data)
    org_by_code = {v: k for k, v in _ORG_CODES.items()}
    kind_by_code = {v: k for k, v in _INDEX_CODES.items()}
    tables = []
    for _ in range(r.u16()):
        name = r.text()
        organization = org_by_code[r.u8()]
        page_size = r.u32()
        fill_factor = r.u16() / 1000
        reorg_ratio = r.u16() / 1000
        auto_reorganize = bool(r.u8())
        columns = []
        for _ in range(r.u8()):
            col_name = r.text()
            col_type = CODE_TYPES[r.u8()]
            size = r.u16()
            primary_key = bool(r.u8())
            columns.append(Column(col_name, col_type, size or None, primary_key))
        files = [r.text() for _ in range(r.u8())]
        indexes = []
        for _ in range(r.u8()):
            idx_name = r.text()
            idx_column = r.text()
            kind = kind_by_code[r.u8()]
            indexes.append(IndexInfo(idx_name, name, idx_column, kind, r.text()))
        tables.append(
            TableInfo(name, columns, organization, page_size, files, indexes, fill_factor, reorg_ratio, auto_reorganize)
        )
    return tables


# ---------------------------------------------------------------------- catálogo
class Catalog:
    """Diccionario de datos persistente (tablas e índices)."""

    def __init__(self, data_dir: str | os.PathLike[str], counter: DiskCounter) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.data_dir / CATALOG_FILE
        self.counter = counter
        self.tables: dict[str, TableInfo] = {}
        self.generation = 0
        if self.path.exists():
            self._load()
        else:
            DiskManager(self.path, CATALOG_PAGE_SIZE, counter, create=True).close()
            self.save()

    # -- persistencia
    def _load(self) -> None:
        with DiskManager(self.path, CATALOG_PAGE_SIZE, self.counter) as dm:
            _, _, meta = _META.unpack(dm.read_page(0))
            length = int(meta["payload_length"])  # type: ignore[arg-type]
            self.generation = int(meta["generation"])  # type: ignore[arg-type]
            chunks = []
            page_id = 1 if meta["data_pages"] else NULL_PAGE
            while page_id != NULL_PAGE:
                data = dm.read_page(page_id)
                header = PageHeader.unpack(data)
                expect_page_type(header, PageType.CATALOG)
                chunks.append(data[PAGE_HEADER_SIZE : PAGE_HEADER_SIZE + header.free_space_offset])
                page_id = header.next_page_id
        payload = b"".join(chunks)
        if len(payload) != length:
            raise CatalogError("catálogo corrupto: longitud del payload inconsistente")
        self.tables = {t.name: t for t in deserialize_tables(payload)}

    def save(self) -> None:
        payload = serialize_tables(list(self.tables.values()))
        chunk = CATALOG_PAGE_SIZE - PAGE_HEADER_SIZE
        pieces = [payload[i : i + chunk] for i in range(0, len(payload), chunk)]
        self.generation += 1
        with DiskManager(self.path, CATALOG_PAGE_SIZE, self.counter) as dm:
            if dm.num_pages() == 0:
                dm.allocate_page()  # página 0; se escribe al final
            for i, piece in enumerate(pieces, start=1):
                buf = bytearray(CATALOG_PAGE_SIZE)
                PageHeader(
                    page_id=i,
                    page_type=PageType.CATALOG,
                    record_count=0,
                    free_space_offset=len(piece),
                    next_page_id=i + 1 if i < len(pieces) else NULL_PAGE,
                ).pack_into(buf)
                buf[PAGE_HEADER_SIZE : PAGE_HEADER_SIZE + len(piece)] = piece
                while dm.num_pages() <= i:
                    dm.allocate_page()
                dm.write_page(i, bytes(buf))
            dm.write_page(
                0,
                _META.pack(
                    CATALOG_PAGE_SIZE,
                    len(pieces) + 1,
                    {"payload_length": len(payload), "data_pages": len(pieces), "generation": self.generation},
                ),
            )
            if dm.num_pages() > len(pieces) + 1:
                dm.truncate(len(pieces) + 1)

    # -- tablas
    def has_table(self, name: str) -> bool:
        return name in self.tables

    def get_table(self, name: str) -> TableInfo:
        try:
            return self.tables[name]
        except KeyError:
            raise CatalogError(f"no existe la tabla '{name}'") from None

    def add_table(self, info: TableInfo) -> None:
        if info.name in self.tables:
            raise CatalogError(f"la tabla '{info.name}' ya existe")
        self.tables[info.name] = info
        self.save()

    def drop_table(self, name: str) -> TableInfo:
        info = self.get_table(name)
        del self.tables[name]
        self.save()
        return info

    # -- índices
    def find_index(self, name: str) -> tuple[TableInfo, IndexInfo]:
        for table in self.tables.values():
            idx = table.index_named(name)
            if idx is not None:
                return table, idx
        raise CatalogError(f"no existe el índice '{name}'")

    def index_exists(self, name: str) -> bool:
        return any(t.index_named(name) for t in self.tables.values())

    def add_index(self, info: IndexInfo) -> None:
        if self.index_exists(info.name):
            raise CatalogError(f"el índice '{info.name}' ya existe")
        self.get_table(info.table).indexes.append(info)
        self.save()

    def drop_index(self, name: str) -> IndexInfo:
        table, idx = self.find_index(name)
        table.indexes.remove(idx)
        self.save()
        return idx

    def update_table(self, info: TableInfo) -> None:
        self.tables[info.name] = info
        self.save()
