"""Formato físico común de las páginas.

Toda página de todo archivo empieza con el mismo header de 24 bytes
(``'<IBBHHiiixx'``, little-endian):

====================  ====  ==========================================================
campo                 tipo  uso
====================  ====  ==========================================================
page_id               u32   id del bloque dentro del archivo
page_type             u8    ver ``PageType``
flags                 u8    específico del tipo (p. ej. ``local_depth`` de un bucket)
record_count          u16   registros o claves activos en la página
free_space_offset     u16   primer slot libre (``0xFFFF`` = llena) o fin de datos usados
next_page_id          i32   encadenamiento (``-1`` = nulo)
prev_page_id          i32   encadenamiento (``-1`` = nulo)
aux_page_id           i32   específico del tipo (p. ej. cabeza del overflow)
(2 bytes de relleno)
====================  ====  ==========================================================

La página 0 de cada archivo es de metadatos (``FILE_META``): después del header
lleva ``magic``, versión, tipo de archivo, ``page_size`` y ``num_pages`` (16
bytes) y luego los campos propios de cada estructura (``MetaCodec``).

Las páginas de datos usan registros de longitud fija con bitmap de presencia:
``[header 24][bitmap ceil(cap/8)][cap * record_size]``.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum
from typing import Iterator

PAGE_HEADER = struct.Struct("<IBBHHiiixx")
PAGE_HEADER_SIZE = PAGE_HEADER.size  # 24
assert PAGE_HEADER_SIZE == 24

NULL_PAGE = -1
FULL_PAGE = 0xFFFF


class PageType(IntEnum):
    FILE_META = 0
    HEAP_DATA = 1
    SEQ_MAIN = 2
    SEQ_OVERFLOW = 3
    BTREE_INTERNAL = 4
    BTREE_LEAF = 5
    HASH_DIR = 6
    HASH_BUCKET = 7
    CATALOG = 8
    SORT_RUN = 9


class FileKind(IntEnum):
    HEAP = 1
    SEQ_MAIN = 2
    SEQ_OVERFLOW = 3
    BPLUS = 4
    HASH = 5
    CATALOG = 6
    SORT_RUN = 7


class PageFormatError(Exception):
    """La página no tiene el formato esperado (tipo, magic o versión incorrectos)."""


class PageFullError(Exception):
    """No quedan slots libres en la página."""


# ---------------------------------------------------------------------- header
@dataclass(slots=True)
class PageHeader:
    page_id: int
    page_type: int
    flags: int = 0
    record_count: int = 0
    free_space_offset: int = 0
    next_page_id: int = NULL_PAGE
    prev_page_id: int = NULL_PAGE
    aux_page_id: int = NULL_PAGE

    def pack(self) -> bytes:
        return PAGE_HEADER.pack(
            self.page_id,
            self.page_type,
            self.flags,
            self.record_count,
            self.free_space_offset,
            self.next_page_id,
            self.prev_page_id,
            self.aux_page_id,
        )

    def pack_into(self, buf: bytearray) -> None:
        PAGE_HEADER.pack_into(
            buf,
            0,
            self.page_id,
            self.page_type,
            self.flags,
            self.record_count,
            self.free_space_offset,
            self.next_page_id,
            self.prev_page_id,
            self.aux_page_id,
        )

    @classmethod
    def unpack(cls, data: bytes | bytearray | memoryview) -> PageHeader:
        return cls(*PAGE_HEADER.unpack_from(data, 0))

    def as_dict(self) -> dict[str, int | str]:
        try:
            type_name = PageType(self.page_type).name
        except ValueError:
            type_name = "UNKNOWN"
        return {
            "page_id": self.page_id,
            "page_type": self.page_type,
            "page_type_name": type_name,
            "flags": self.flags,
            "record_count": self.record_count,
            "free_space_offset": self.free_space_offset,
            "next_page_id": self.next_page_id,
            "prev_page_id": self.prev_page_id,
            "aux_page_id": self.aux_page_id,
        }


def expect_page_type(header: PageHeader, *expected: PageType) -> None:
    if header.page_type not in expected:
        names = "/".join(t.name for t in expected)
        raise PageFormatError(
            f"página {header.page_id}: se esperaba {names} y se encontró tipo {header.page_type}"
        )


# ---------------------------------------------------------------------- metadatos
FILE_MAGIC = b"MDB1"
FILE_VERSION = 1
META_COMMON = struct.Struct("<4sHHII")  # magic, version, file_kind, page_size, num_pages
META_COMMON_OFFSET = PAGE_HEADER_SIZE  # 24
META_SPECIFIC_OFFSET = META_COMMON_OFFSET + META_COMMON.size  # 40
PAGE_SIZE_OFFSET = META_COMMON_OFFSET + 8  # donde está page_size dentro de la página 0


class MetaCodec:
    """Empaqueta la página 0 (``FILE_META``) de un tipo de archivo.

    ``fields`` es una lista ``(nombre, formato_struct)``. Un formato con repetición
    (p. ej. ``'21i'``) produce una lista de valores.
    """

    def __init__(self, kind: FileKind, fields: list[tuple[str, str]]) -> None:
        self.kind = kind
        self.fields = fields
        self.struct = struct.Struct("<" + "".join(fmt for _, fmt in fields))
        self._arity: list[int] = []
        for _, fmt in fields:
            digits = fmt[:-1]
            self._arity.append(int(digits) if digits and fmt[-1] != "s" else 1)

    def pack(self, page_size: int, num_pages: int, values: dict[str, object]) -> bytes:
        buf = bytearray(page_size)
        if META_SPECIFIC_OFFSET + self.struct.size > page_size:
            raise PageFormatError("los metadatos no caben en la página 0")
        PageHeader(page_id=0, page_type=PageType.FILE_META).pack_into(buf)
        META_COMMON.pack_into(buf, META_COMMON_OFFSET, FILE_MAGIC, FILE_VERSION, self.kind, page_size, num_pages)
        flat: list[object] = []
        for (name, _), arity in zip(self.fields, self._arity):
            value = values[name]
            if arity == 1:
                flat.append(value)
            else:
                items = list(value)  # type: ignore[arg-type]
                if len(items) != arity:
                    raise PageFormatError(f"{name}: se esperaban {arity} valores")
                flat.extend(items)
        self.struct.pack_into(buf, META_SPECIFIC_OFFSET, *flat)
        return bytes(buf)

    def unpack(self, data: bytes) -> tuple[int, int, dict[str, object]]:
        """Devuelve ``(page_size, num_pages, campos)`` validando magic, versión y tipo."""
        header = PageHeader.unpack(data)
        expect_page_type(header, PageType.FILE_META)
        magic, version, kind, page_size, num_pages = META_COMMON.unpack_from(data, META_COMMON_OFFSET)
        if magic != FILE_MAGIC:
            raise PageFormatError(f"magic inválido: {magic!r}")
        if version != FILE_VERSION:
            raise PageFormatError(f"versión de formato no soportada: {version}")
        if kind != self.kind:
            raise PageFormatError(f"se esperaba un archivo {self.kind.name} y es de tipo {kind}")
        flat = self.struct.unpack_from(data, META_SPECIFIC_OFFSET)
        values: dict[str, object] = {}
        pos = 0
        for (name, _), arity in zip(self.fields, self._arity):
            if arity == 1:
                values[name] = flat[pos]
            else:
                values[name] = list(flat[pos : pos + arity])
            pos += arity
        return page_size, num_pages, values


def read_common_meta(data: bytes) -> dict[str, object]:
    """Decodifica solo la parte común de una página 0 (para herramientas de inspección)."""
    header = PageHeader.unpack(data)
    expect_page_type(header, PageType.FILE_META)
    magic, version, kind, page_size, num_pages = META_COMMON.unpack_from(data, META_COMMON_OFFSET)
    if magic != FILE_MAGIC:
        raise PageFormatError(f"magic inválido: {magic!r}")
    try:
        kind_name = FileKind(kind).name
    except ValueError:
        kind_name = "UNKNOWN"
    return {
        "magic": magic.decode("ascii", errors="replace"),
        "version": version,
        "file_kind": kind,
        "file_kind_name": kind_name,
        "page_size": page_size,
        "num_pages": num_pages,
    }


# ---------------------------------------------------------------------- bitmap
def bitmap_size(capacity: int) -> int:
    return (capacity + 7) // 8


def bit_is_set(buf: bytes | bytearray, bitmap_offset: int, i: int) -> bool:
    return bool(buf[bitmap_offset + (i >> 3)] >> (i & 7) & 1)


def set_bit(buf: bytearray, bitmap_offset: int, i: int) -> None:
    buf[bitmap_offset + (i >> 3)] |= 1 << (i & 7)


def clear_bit(buf: bytearray, bitmap_offset: int, i: int) -> None:
    buf[bitmap_offset + (i >> 3)] &= ~(1 << (i & 7)) & 0xFF


def first_clear_bit(buf: bytes | bytearray, bitmap_offset: int, capacity: int, start: int = 0) -> int:
    """Primer slot libre ``>= start`` o ``-1`` si no hay. Salta bytes llenos (0xFF)."""
    i = start
    while i < capacity:
        byte = buf[bitmap_offset + (i >> 3)]
        if byte == 0xFF and (i & 7) == 0:
            i += 8
            continue
        if not byte >> (i & 7) & 1:
            return i
        i += 1
    return -1


def iter_set_bits(buf: bytes | bytearray, bitmap_offset: int, capacity: int) -> Iterator[int]:
    """Slots ocupados en orden ascendente."""
    for byte_index in range(bitmap_size(capacity)):
        byte = buf[bitmap_offset + byte_index]
        if not byte:
            continue
        base = byte_index << 3
        for bit in range(8):
            if byte >> bit & 1:
                slot = base + bit
                if slot < capacity:
                    yield slot


# ---------------------------------------------------------------------- páginas de registros
def record_page_capacity(page_size: int, record_size: int) -> int:
    """Máximo ``n`` tal que ``24 + ceil(n/8) + n * record_size <= page_size``."""
    if record_size <= 0:
        raise ValueError("record_size debe ser positivo")
    usable = page_size - PAGE_HEADER_SIZE
    n = usable * 8 // (8 * record_size + 1)
    while n > 0 and PAGE_HEADER_SIZE + bitmap_size(n) + n * record_size > page_size:
        n -= 1
    return n


class RecordPageLayout:
    """Geometría de una página de registros de longitud fija con bitmap."""

    __slots__ = ("page_size", "record_size", "capacity", "bitmap_offset", "slots_offset")

    def __init__(self, page_size: int, record_size: int) -> None:
        self.page_size = page_size
        self.record_size = record_size
        self.capacity = record_page_capacity(page_size, record_size)
        if self.capacity < 1:
            raise PageFormatError(
                f"un registro de {record_size} bytes no cabe en una página de {page_size} bytes"
            )
        self.bitmap_offset = PAGE_HEADER_SIZE
        self.slots_offset = PAGE_HEADER_SIZE + bitmap_size(self.capacity)

    def slot_offset(self, slot: int) -> int:
        return self.slots_offset + slot * self.record_size


class RecordPage:
    """Página de datos (HEAP_DATA, SEQ_MAIN, SEQ_OVERFLOW o SORT_RUN) en memoria.

    ``free_space_offset`` tiene dos semánticas:

    * HEAP_DATA / SEQ_OVERFLOW / SORT_RUN: índice del primer slot libre (los slots
      borrados se reutilizan).
    * SEQ_MAIN: marca de agua (primer slot nunca usado desde la última
      reorganización). Los slots borrados NO se reutilizan para que el slot 0
      conserve la clave frontera que guía la búsqueda binaria.

    En ambos casos ``0xFFFF`` significa que no hay dónde insertar.
    """

    __slots__ = ("layout", "buf", "header")

    def __init__(self, layout: RecordPageLayout, buf: bytearray, header: PageHeader) -> None:
        self.layout = layout
        self.buf = buf
        self.header = header

    @classmethod
    def new(cls, layout: RecordPageLayout, page_id: int, page_type: PageType) -> RecordPage:
        buf = bytearray(layout.page_size)
        header = PageHeader(page_id=page_id, page_type=page_type, free_space_offset=0)
        return cls(layout, buf, header)

    @classmethod
    def from_bytes(cls, layout: RecordPageLayout, data: bytes, *expected: PageType) -> RecordPage:
        header = PageHeader.unpack(data)
        if expected:
            expect_page_type(header, *expected)
        return cls(layout, bytearray(data), header)

    def to_bytes(self) -> bytes:
        self.header.pack_into(self.buf)
        return bytes(self.buf)

    # -- consultas
    @property
    def page_id(self) -> int:
        return self.header.page_id

    @property
    def reuses_slots(self) -> bool:
        return self.header.page_type != PageType.SEQ_MAIN

    def has_space(self) -> bool:
        return self.header.free_space_offset != FULL_PAGE

    def is_used(self, slot: int) -> bool:
        return bit_is_set(self.buf, self.layout.bitmap_offset, slot)

    def used_slots(self) -> Iterator[int]:
        return iter_set_bits(self.buf, self.layout.bitmap_offset, self.layout.capacity)

    def get(self, slot: int) -> bytes:
        off = self.layout.slot_offset(slot)
        return bytes(self.buf[off : off + self.layout.record_size])

    def records(self) -> Iterator[tuple[int, bytes]]:
        rs = self.layout.record_size
        base = self.layout.slots_offset
        buf = self.buf
        for slot in self.used_slots():
            off = base + slot * rs
            yield slot, bytes(buf[off : off + rs])

    def high_water_mark(self) -> int:
        """Slots usados alguna vez en una página SEQ_MAIN (``capacity`` si está llena)."""
        fso = self.header.free_space_offset
        return self.layout.capacity if fso == FULL_PAGE else fso

    # -- modificaciones
    def insert(self, record: bytes) -> int:
        """Guarda ``record`` en el slot indicado por ``free_space_offset`` y lo devuelve."""
        slot = self.header.free_space_offset
        if slot == FULL_PAGE:
            raise PageFullError(f"página {self.page_id} llena")
        self._write_slot(slot, record)
        cap = self.layout.capacity
        if self.reuses_slots:
            nxt = first_clear_bit(self.buf, self.layout.bitmap_offset, cap, slot + 1)
            self.header.free_space_offset = FULL_PAGE if nxt < 0 else nxt
        else:
            self.header.free_space_offset = FULL_PAGE if slot + 1 >= cap else slot + 1
        return slot

    def _write_slot(self, slot: int, record: bytes) -> None:
        if len(record) != self.layout.record_size:
            raise PageFormatError(f"registro de {len(record)} bytes (se esperaban {self.layout.record_size})")
        if self.is_used(slot):
            raise PageFormatError(f"slot {slot} de la página {self.page_id} ya está ocupado")
        off = self.layout.slot_offset(slot)
        self.buf[off : off + len(record)] = record
        set_bit(self.buf, self.layout.bitmap_offset, slot)
        self.header.record_count += 1

    def delete(self, slot: int) -> None:
        """Borrado lógico: limpia el bit de presencia (los bytes quedan como lápida)."""
        if not 0 <= slot < self.layout.capacity or not self.is_used(slot):
            raise KeyError(f"slot {slot} de la página {self.page_id} no está ocupado")
        clear_bit(self.buf, self.layout.bitmap_offset, slot)
        self.header.record_count -= 1
        if self.reuses_slots:
            fso = self.header.free_space_offset
            if fso == FULL_PAGE or slot < fso:
                self.header.free_space_offset = slot

    def replace(self, slot: int, record: bytes) -> None:
        """Sobrescribe un registro ocupado (usado solo por herramientas y tests)."""
        if not self.is_used(slot):
            raise KeyError(f"slot {slot} de la página {self.page_id} no está ocupado")
        off = self.layout.slot_offset(slot)
        self.buf[off : off + self.layout.record_size] = record
