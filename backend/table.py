"""Una tabla abierta: su archivo (Heap o Sequential) más sus índices.

Mantiene la consistencia entre archivo e índices:

* ``insert``: valida tipos, inserta en el archivo y en todos los índices. Si un
  índice único (el de la PK) detecta un duplicado, deshace lo hecho y falla.
  En SEQUENTIAL la unicidad de la PK la valida el propio archivo; en HEAP solo
  se valida si hay un índice sobre la PK (con un full scan por inserción el
  Experimento 1 sería O(N²)).
* ``delete_*``: borra del archivo y quita las entradas de todos los índices.
* ``reorganize`` (SEQUENTIAL): reescribe el archivo y reconstruye los índices,
  porque los RIDs cambian.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterator, Sequence

from backend.files.common import DuplicateKeyError, RawFilter
from backend.files.heap_file import HeapFile
from backend.files.sequential_file import ReorganizeStats, SequentialFile
from backend.indexes.bplus_tree import BPlusTree
from backend.indexes.extendible_hash import ExtendibleHash
from backend.storage.catalog import IndexInfo, IndexKind, Organization, TableInfo
from backend.storage.disk_manager import DiskCounter
from backend.storage.record import Schema
from backend.storage.rid import RID

FILE_EXTENSIONS = {Organization.HEAP: ("heap",), Organization.SEQUENTIAL: ("seq", "ovf")}
INDEX_EXTENSIONS = {IndexKind.BTREE: "bpt", IndexKind.HASH: "hsh"}


def table_files(name: str, organization: Organization) -> list[str]:
    return [f"{name}.{ext}" for ext in FILE_EXTENSIONS[organization]]


def index_file(table: str, column: str, kind: IndexKind) -> str:
    return f"{table}_{column}.{INDEX_EXTENSIONS[kind]}"


class Index:
    """Índice abierto (B+ o hash) sobre una columna de la tabla."""

    def __init__(self, info: IndexInfo, table: TableInfo, data_dir: Path, counter: DiskCounter, *, create: bool) -> None:
        self.info = info
        schema = table.schema
        self.column_index = schema.index_of(info.column)
        self.column = schema.columns[self.column_index]
        self.unique = schema.pk_index == self.column_index
        self.path = data_dir / info.file
        cls = BPlusTree if info.kind == IndexKind.BTREE else ExtendibleHash
        if create:
            if self.path.exists():
                self.path.unlink()  # archivo huérfano de una ejecución anterior
            self.structure: BPlusTree | ExtendibleHash = cls.create(
                self.path, self.column, table.page_size, counter, unique=self.unique
            )
        else:
            self.structure = cls.open(self.path, self.column, table.page_size, counter)

    @property
    def name(self) -> str:
        return self.info.name

    @property
    def kind(self) -> IndexKind:
        return self.info.kind

    def insert(self, values: Sequence[Any], rid: RID) -> None:
        self.structure.insert(values[self.column_index], rid)

    def delete(self, values: Sequence[Any], rid: RID) -> bool:
        return self.structure.delete(values[self.column_index], rid)

    def search(self, value: Any) -> list[RID]:
        return self.structure.search(value)

    def range(self, low: Any, high: Any, low_inclusive: bool, high_inclusive: bool) -> Iterator[RID]:
        assert isinstance(self.structure, BPlusTree)
        for _, rid in self.structure.range_search(
            low, high, low_inclusive=low_inclusive, high_inclusive=high_inclusive
        ):
            yield rid

    def flush(self) -> None:
        self.structure.flush()

    def sync(self) -> None:
        self.structure.sync()

    def close(self) -> None:
        self.structure.close()

    def stats(self) -> dict[str, Any]:
        s = self.structure
        return {
            "name": self.name,
            "column": self.info.column,
            "type": self.kind.value,
            "unique": self.unique,
            "height": s.height if isinstance(s, BPlusTree) else None,
            "global_depth": s.global_depth if isinstance(s, ExtendibleHash) else None,
            "page_count": s.page_count,
            "entry_count": s.entry_count,
            "file": self.info.file,
        }


class Table:
    def __init__(self, info: TableInfo, data_dir: Path, counter: DiskCounter, *, create: bool = False) -> None:
        self.info = info
        self.data_dir = Path(data_dir)
        self.counter = counter
        self.schema: Schema = info.schema
        paths = [self.data_dir / f for f in info.files]
        if create:
            for path in paths:
                if path.exists():
                    path.unlink()  # restos huérfanos (no están en el catálogo)
        self.file: HeapFile | SequentialFile
        if info.organization == Organization.HEAP:
            opener = HeapFile.create if create else HeapFile.open
            self.file = opener(paths[0], self.schema, info.page_size, counter)
        else:
            kwargs = {"reorg_ratio": info.reorg_ratio, "auto_reorganize": info.auto_reorganize}
            if create:
                self.file = SequentialFile.create(
                    paths[0], paths[1], self.schema, info.page_size, counter, fill_factor=info.fill_factor, **kwargs
                )
            else:
                self.file = SequentialFile.open(paths[0], paths[1], self.schema, info.page_size, counter, **kwargs)
        self.indexes: dict[str, Index] = {
            idx.name: Index(idx, info, self.data_dir, counter, create=False) for idx in info.indexes
        }

    # ------------------------------------------------------------------ propiedades
    @property
    def name(self) -> str:
        return self.info.name

    @property
    def organization(self) -> Organization:
        return self.info.organization

    @property
    def is_sequential(self) -> bool:
        return self.info.organization == Organization.SEQUENTIAL

    def indexes_on(self, column_index: int) -> list[Index]:
        return [i for i in self.indexes.values() if i.column_index == column_index]

    # ------------------------------------------------------------------ escritura
    def insert(self, values: Sequence[Any]) -> tuple[RID, bool]:
        """Inserta una fila (ya validada con ``schema.coerce``).

        Devuelve el RID y si se disparó una reorganización automática.
        """
        rid = self.file.insert(values) if isinstance(self.file, HeapFile) else self.file.insert(values, check_unique=True)
        done: list[Index] = []
        try:
            # Primero los índices únicos: si hay duplicado se detecta antes de tocar los demás.
            for idx in sorted(self.indexes.values(), key=lambda i: not i.unique):
                idx.insert(values, rid)
                done.append(idx)
        except DuplicateKeyError:
            for idx in done:
                idx.delete(values, rid)
            self.file.delete(rid)
            raise
        if isinstance(self.file, SequentialFile) and self.file.needs_reorganization():
            self.reorganize()
            return rid, True
        return rid, False

    def _unindex(self, deleted: Sequence[tuple[RID, tuple[Any, ...]]]) -> None:
        for idx in self.indexes.values():
            for rid, values in deleted:
                idx.delete(values, rid)

    def delete_rids(self, rids: Sequence[RID]) -> list[tuple[RID, tuple[Any, ...]]]:
        deleted = self.file.delete_many(rids)
        self._unindex(deleted)
        return deleted

    def delete_where(self, raw_filter: RawFilter | None) -> int:
        """Borra en un solo recorrido del archivo (SeqScan) y actualiza los índices."""
        count = 0
        batch: list[tuple[RID, tuple[Any, ...]]] = []
        for item in self.file.delete_where(raw_filter):
            batch.append(item)
            count += 1
            if len(batch) >= 1024:
                self._unindex(batch)
                batch = []
        self._unindex(batch)
        return count

    def fetch(self, rid: RID) -> tuple[Any, ...]:
        return self.file.read(rid)

    def scan(self, raw_filter: RawFilter | None = None) -> Iterator[tuple[RID, tuple[Any, ...]]]:
        return self.file.scan(raw_filter)

    # ------------------------------------------------------------------ índices
    def add_index(self, info: IndexInfo) -> Index:
        """Crea el archivo del índice y lo construye recorriendo la tabla."""
        index = Index(info, self.info, self.data_dir, self.counter, create=True)
        try:
            self._build(index)
        except Exception:
            index.close()
            index.path.unlink(missing_ok=True)
            raise
        self.indexes[info.name] = index
        return index

    def _build(self, index: Index) -> None:
        col = index.column_index
        sorted_scan = self.is_sequential and col == self.schema.pk_index and index.kind == IndexKind.BTREE
        if sorted_scan:
            # El Sequential se recorre en orden de PK: carga masiva de abajo hacia arriba.
            assert isinstance(index.structure, BPlusTree)
            index.structure.bulk_load((values[col], rid) for rid, values in self.file.scan())
        else:
            for rid, values in self.file.scan():
                index.insert(values, rid)

    def remove_index(self, name: str) -> None:
        index = self.indexes.pop(name)
        index.close()
        index.path.unlink(missing_ok=True)

    def rebuild_indexes(self) -> None:
        for name in list(self.indexes):
            old = self.indexes[name]
            old.close()
            fresh = Index(old.info, self.info, self.data_dir, self.counter, create=True)
            self._build(fresh)
            self.indexes[name] = fresh

    # ------------------------------------------------------------------ mantenimiento
    def reorganize(self) -> ReorganizeStats:
        if not isinstance(self.file, SequentialFile):
            raise TypeError("solo las tablas SEQUENTIAL se reorganizan")
        stats = self.file.reorganize()
        self.rebuild_indexes()
        return stats

    def flush(self) -> None:
        """Fin de sentencia: persiste las páginas 0 que cambiaron estructuralmente."""
        self.file.flush()
        for idx in self.indexes.values():
            idx.flush()

    def sync(self) -> None:
        self.file.sync()
        for idx in self.indexes.values():
            idx.sync()

    def close(self) -> None:
        self.file.close()
        for idx in self.indexes.values():
            idx.close()

    def drop_files(self) -> None:
        self.close()
        for f in self.info.files:
            (self.data_dir / f).unlink(missing_ok=True)
        for idx in self.indexes.values():
            idx.path.unlink(missing_ok=True)

    # ------------------------------------------------------------------ estadísticas
    def stats(self) -> dict[str, Any]:
        f = self.file
        seq = isinstance(f, SequentialFile)
        return {
            "name": self.name,
            "organization": self.organization.value,
            "page_size": self.info.page_size,
            "primary_key": None if self.schema.primary_key is None else self.schema.primary_key.name,
            "columns": [
                {
                    "name": c.name,
                    "type": c.type.value,
                    "size": c.size,
                    "primary_key": c.primary_key,
                }
                for c in self.schema.columns
            ],
            "record_size": self.schema.record_size,
            "records_per_page": f.capacity,
            "record_count": f.record_count,
            "page_count": f.main_pages if seq else f.page_count,
            "overflow_page_count": f.overflow_pages if seq else None,
            "fill_factor": f.fill_factor if seq else None,
            "reorganizations": f.reorganizations if seq else None,
            "files": list(self.info.files),
            "indexes": [idx.stats() for idx in self.indexes.values()],
        }

    def file_size_bytes(self) -> int:
        total = sum(os.path.getsize(self.data_dir / f) for f in self.info.files)
        return total + sum(os.path.getsize(idx.path) for idx in self.indexes.values())
