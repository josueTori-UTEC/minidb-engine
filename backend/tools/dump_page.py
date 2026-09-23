"""Inspección de páginas binarias (para el video y el inspector del frontend).

Uso::

    python -m backend.tools.dump_page data/empleados.heap 1
    python -m backend.tools.dump_page data/empleados_id.bpt 0       # página de metadatos
    python -m backend.tools.dump_page data/empleados.seq --all      # un renglón por página

Decodifica el header común de 24 bytes, los campos propios de cada tipo de
página (bitmap y registros, claves e hijos del B+, directorio y buckets del hash)
y muestra un hexdump al estilo ``hexdump -C``. Si la carpeta del archivo tiene un
``catalog.bin``, usa el esquema de la tabla para decodificar los registros.
Las lecturas se hacen con un ``DiskManager`` propio (no afectan las métricas de
las consultas).
"""

from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path
from typing import Any

from backend.files.heap_file import HEAP_META
from backend.files.sequential_file import OVF_META, RUN_META, SEQ_META
from backend.indexes.bplus_tree import BPT_META
from backend.indexes.extendible_hash import HASH_META
from backend.storage.catalog import CATALOG_FILE, Catalog
from backend.storage.disk_manager import MIN_PAGE_SIZE, DiskCounter, DiskManager
from backend.storage.page import (
    PAGE_HEADER_SIZE,
    FileKind,
    MetaCodec,
    PageHeader,
    PageType,
    RecordPageLayout,
    iter_set_bits,
    read_common_meta,
)
from backend.storage.record import CODE_TYPES, Column, Schema, decode_str
from backend.storage.rid import RID_SIZE, RID_STRUCT

_CATALOG_META = MetaCodec(FileKind.CATALOG, [("payload_length", "I"), ("data_pages", "I"), ("generation", "I")])
META_CODECS: dict[int, MetaCodec] = {
    FileKind.HEAP: HEAP_META,
    FileKind.SEQ_MAIN: SEQ_META,
    FileKind.SEQ_OVERFLOW: OVF_META,
    FileKind.SORT_RUN: RUN_META,
    FileKind.BPLUS: BPT_META,
    FileKind.HASH: HASH_META,
    FileKind.CATALOG: _CATALOG_META,
}
MAX_ROWS_IN_DETAILS = 200


def hexdump(data: bytes, width: int = 16) -> str:
    """Hexdump estilo ``hexdump -C``; las líneas repetidas se resumen con ``*``."""
    lines = []
    previous = None
    starred = False
    for offset in range(0, len(data), width):
        chunk = data[offset : offset + width]
        if chunk == previous and offset + width < len(data):
            if not starred:
                lines.append("*")
                starred = True
            continue
        previous, starred = chunk, False
        hex_part = " ".join(f"{b:02x}" for b in chunk[:8]) + "  " + " ".join(f"{b:02x}" for b in chunk[8:])
        ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"{offset:08x}  {hex_part:<48}  |{ascii_part}|")
    lines.append(f"{len(data):08x}")
    return "\n".join(lines)


def probe_page_size(path: Path) -> int:
    """Lee el ``page_size`` guardado en la página 0 (lectura mínima de 512 bytes)."""
    with DiskManager(path, MIN_PAGE_SIZE, DiskCounter()) as dm:
        head = dm.read_page(0)
    return int(read_common_meta(head)["page_size"])


def schema_from_catalog(path: Path) -> Schema | None:
    catalog_path = path.parent / CATALOG_FILE
    if not catalog_path.exists():
        return None
    catalog = Catalog(path.parent, DiskCounter())
    for table in catalog.tables.values():
        if path.name in table.files:
            return table.schema
    return None


def _jsonable(value: Any) -> Any:
    if isinstance(value, bytes):
        return decode_str(value)
    if isinstance(value, float) and (value != value):
        return None
    return value


def _key_column(meta: dict[str, Any]) -> Column:
    ctype = CODE_TYPES[int(meta["key_type"])]
    size = int(meta["key_size"])
    return Column("key", ctype, size if ctype.value == "CHAR" else None)


def _record_details(
    data: bytes, header: PageHeader, meta: dict[str, Any], page_size: int, schema: Schema | None
) -> dict[str, Any]:
    layout = RecordPageLayout(page_size, int(meta["record_size"]))
    used = list(iter_set_bits(data, layout.bitmap_offset, layout.capacity))
    details: dict[str, Any] = {
        "capacity": layout.capacity,
        "record_size": layout.record_size,
        "bitmap_bytes": layout.slots_offset - layout.bitmap_offset,
        "bitmap_hex": data[layout.bitmap_offset : layout.slots_offset].hex(" "),
        "occupied_slots": used,
    }
    if header.page_type == PageType.SEQ_MAIN:
        hwm = layout.capacity if header.free_space_offset == 0xFFFF else header.free_space_offset
        details["high_water_mark"] = hwm
        details["tombstones"] = [s for s in range(hwm) if s not in set(used)]
        details["overflow_head"] = header.aux_page_id
    records = []
    for slot in used[:MAX_ROWS_IN_DETAILS]:
        off = layout.slot_offset(slot)
        raw = data[off : off + layout.record_size]
        if schema is not None and schema.record_size == layout.record_size:
            records.append({"slot": slot, "offset": off, "values": [_jsonable(v) for v in schema.decode(raw)]})
        else:
            records.append({"slot": slot, "offset": off, "hex": raw.hex()})
    details["records"] = records
    if schema is not None and header.page_type == PageType.SEQ_MAIN and schema.pk_index is not None and used + details["tombstones"]:
        fence_raw = data[layout.slots_offset : layout.slots_offset + layout.record_size]
        details["fence_key"] = _jsonable(schema.decode(fence_raw)[schema.pk_index])
    return details


def _btree_details(data: bytes, header: PageHeader, meta: dict[str, Any]) -> dict[str, Any]:
    col = _key_column(meta)
    n = header.record_count
    if header.page_type == PageType.BTREE_LEAF:
        vals = struct.unpack_from("<" + (col.fmt + "iH") * n, data, PAGE_HEADER_SIZE)
        entries = [
            {"key": _jsonable(vals[i]), "rid": [vals[i + 1], vals[i + 2]]} for i in range(0, 3 * n, 3)
        ]
        return {
            "kind": "hoja",
            "keys": n,
            "capacity": int(meta["leaf_capacity"]),
            "next_leaf": header.next_page_id,
            "prev_leaf": header.prev_page_id,
            "entries": entries[:MAX_ROWS_IN_DETAILS],
        }
    vals = struct.unpack_from("<i" + (col.fmt + "i") * n, data, PAGE_HEADER_SIZE)
    return {
        "kind": "interno",
        "keys": [_jsonable(k) for k in vals[1::2]],
        "children": list(vals[0::2]),
        "capacity": int(meta["internal_capacity"]),
    }


def _hash_details(data: bytes, header: PageHeader, meta: dict[str, Any], page_size: int) -> dict[str, Any]:
    if header.page_type == PageType.HASH_DIR:
        per_page = (page_size - PAGE_HEADER_SIZE) // 4
        entries = struct.unpack_from(f"<{per_page}i", data, PAGE_HEADER_SIZE)[: header.record_count]
        return {
            "kind": "directorio",
            "dir_page_index": header.aux_page_id,
            "global_depth": header.flags,
            "first_entry": header.aux_page_id * per_page,
            "pointers": list(entries[:MAX_ROWS_IN_DETAILS * 4]),
        }
    col = _key_column(meta)
    ks = col.byte_size
    es = ks + RID_SIZE
    entries = []
    for i in range(header.record_count):
        off = PAGE_HEADER_SIZE + i * es
        key = struct.unpack_from("<" + col.fmt, data, off)[0]
        entries.append({"key": _jsonable(key), "rid": list(RID_STRUCT.unpack_from(data, off + ks))})
    return {
        "kind": "bucket",
        "local_depth": header.flags,
        "overflow_next": header.next_page_id,
        "capacity": int(meta["bucket_capacity"]),
        "entries": entries[:MAX_ROWS_IN_DETAILS],
    }


def inspect_page(
    path: str | Path, page_id: int, page_size: int | None = None, schema: Schema | None = None
) -> dict[str, Any]:
    """Decodifica una página de cualquier archivo del motor."""
    path = Path(path)
    page_size = page_size or probe_page_size(path)
    with DiskManager(path, page_size, DiskCounter()) as dm:
        num_pages = dm.num_pages()
        meta_page = dm.read_page(0)
        data = meta_page if page_id == 0 else dm.read_page(page_id)
    common = read_common_meta(meta_page)
    kind = int(common["file_kind"])
    codec = META_CODECS.get(kind)
    meta: dict[str, Any] = {}
    if codec is not None:
        _, _, meta = codec.unpack(meta_page)
    header = PageHeader.unpack(data)
    ptype = header.page_type
    if page_id == 0:
        details: dict[str, Any] = {"common": common, "fields": {k: _jsonable(v) for k, v in meta.items()}}
        if kind == FileKind.HASH:
            n = int(meta["segment_count"])
            details["fields"]["segments"] = [
                {"first_dir_page": meta["seg_first_index"][i], "first_page_id": meta["seg_first_page"][i],
                 "pages": meta["seg_pages"][i]}
                for i in range(n)
            ]
            for k in ("seg_first_index", "seg_first_page", "seg_pages"):
                details["fields"].pop(k, None)
    elif ptype in (PageType.HEAP_DATA, PageType.SEQ_MAIN, PageType.SEQ_OVERFLOW, PageType.SORT_RUN):
        details = _record_details(data, header, meta, page_size, schema)
    elif ptype in (PageType.BTREE_LEAF, PageType.BTREE_INTERNAL):
        details = _btree_details(data, header, meta)
    elif ptype in (PageType.HASH_DIR, PageType.HASH_BUCKET):
        details = _hash_details(data, header, meta, page_size)
    elif ptype == PageType.CATALOG:
        payload = data[PAGE_HEADER_SIZE : PAGE_HEADER_SIZE + header.free_space_offset]
        details = {"payload_bytes": len(payload), "payload_preview": payload[:160].decode("latin-1")}
    else:
        details = {}
    return {
        "path": path.name,
        "page_id": page_id,
        "page_size": page_size,
        "num_pages": num_pages,
        "file_kind": common["file_kind_name"],
        "header": header.as_dict(),
        "details": details,
        "hexdump": hexdump(data),
    }


def page_summary(path: str | Path, page_size: int | None = None) -> list[dict[str, Any]]:
    """Header de todas las páginas del archivo (modo ``--all``)."""
    path = Path(path)
    page_size = page_size or probe_page_size(path)
    rows = []
    with DiskManager(path, page_size, DiskCounter()) as dm:
        for pid in range(dm.num_pages()):
            rows.append(PageHeader.unpack(dm.read_page(pid)).as_dict())
    return rows


def _print_page(info: dict[str, Any]) -> None:
    print(f"{info['path']}  página {info['page_id']} de {info['num_pages']}  (page_size={info['page_size']}, "
          f"archivo {info['file_kind']})")
    print("\nHeader (24 bytes, '<IBBHHiiixx'):")
    for key, value in info["header"].items():
        print(f"  {key:<18} {value}")
    print("\nContenido:")
    for key, value in info["details"].items():
        if isinstance(value, list) and len(value) > 12:
            print(f"  {key:<18} [{len(value)} elementos] {value[:12]} ...")
        else:
            print(f"  {key:<18} {value}")
    print("\nHexdump:")
    print(info["hexdump"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspecciona páginas de los archivos binarios de MiniDB")
    parser.add_argument("file", type=Path, help="archivo .heap, .seq, .ovf, .bpt, .hsh o catalog.bin")
    parser.add_argument("page_id", type=int, nargs="?", default=0, help="página a mostrar (0 = metadatos)")
    parser.add_argument("--page-size", type=int, default=None, help="se lee de la página 0 si se omite")
    parser.add_argument("--all", action="store_true", help="lista el header de todas las páginas")
    args = parser.parse_args(argv)
    if not args.file.exists():
        print(f"no existe {args.file}", file=sys.stderr)
        return 1
    if args.all:
        rows = page_summary(args.file, args.page_size)
        print(f"{'page':>6} {'tipo':<15} {'flags':>5} {'count':>6} {'free':>6} {'next':>7} {'prev':>7} {'aux':>7}")
        for r in rows:
            print(f"{r['page_id']:>6} {r['page_type_name']:<15} {r['flags']:>5} {r['record_count']:>6} "
                  f"{r['free_space_offset']:>6} {r['next_page_id']:>7} {r['prev_page_id']:>7} {r['aux_page_id']:>7}")
        return 0
    _print_page(inspect_page(args.file, args.page_id, args.page_size, schema_from_catalog(args.file)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
