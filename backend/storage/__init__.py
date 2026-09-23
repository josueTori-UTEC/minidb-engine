"""Capa de almacenamiento: disco paginado, formato de páginas, registros y RID."""

from backend.storage.disk_manager import DEFAULT_PAGE_SIZE, DiskCounter, DiskError, DiskManager
from backend.storage.page import (
    FULL_PAGE,
    NULL_PAGE,
    PAGE_HEADER_SIZE,
    FileKind,
    MetaCodec,
    PageHeader,
    PageType,
    RecordPage,
    RecordPageLayout,
)
from backend.storage.record import Column, ColumnType, KeyCodec, Schema, SchemaError
from backend.storage.rid import RID

__all__ = [
    "DEFAULT_PAGE_SIZE",
    "FULL_PAGE",
    "NULL_PAGE",
    "PAGE_HEADER_SIZE",
    "RID",
    "Column",
    "ColumnType",
    "DiskCounter",
    "DiskError",
    "DiskManager",
    "FileKind",
    "KeyCodec",
    "MetaCodec",
    "PageHeader",
    "PageType",
    "RecordPage",
    "RecordPageLayout",
    "Schema",
    "SchemaError",
]
