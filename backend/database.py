"""Punto de entrada del motor: catálogo + tablas abiertas + ejecución de SQL.

``Database.execute`` parsea el script sentencia por sentencia y, para cada una,
resetea el ``DiskCounter`` compartido, la ejecuta, persiste las páginas 0 que
cambiaron (fin de sentencia) y reporta lecturas, escrituras y tiempos
(``time.perf_counter()`` en ms). Un lock serializa las operaciones: el motor
atiende una sentencia a la vez, así el conteo de I/O de cada una es exacto.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any

from backend.files.sequential_file import DEFAULT_FILL_FACTOR, DEFAULT_REORG_RATIO, SequentialFile
from backend.sql.errors import SemanticError
from backend.sql.executor import StatementResult, execute_statement
from backend.sql.parser import Parser
from backend.sql.planner import PLANNER_MODES
from backend.storage.catalog import Catalog
from backend.storage.disk_manager import DEFAULT_PAGE_SIZE, DiskCounter, validate_page_size
from backend.table import Table


def _ms(seconds: float) -> float:
    return round(seconds * 1000, 3)


class Database:
    def __init__(
        self,
        data_dir: str | os.PathLike[str],
        *,
        page_size: int = DEFAULT_PAGE_SIZE,
        planner_mode: str = "rules",
        fill_factor: float = DEFAULT_FILL_FACTOR,
        reorg_ratio: float = DEFAULT_REORG_RATIO,
    ) -> None:
        if planner_mode not in PLANNER_MODES:
            raise ValueError(f"planner_mode debe ser uno de {PLANNER_MODES}")
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.page_size = validate_page_size(page_size)
        self.planner_mode = planner_mode
        self.fill_factor = fill_factor
        self.reorg_ratio = reorg_ratio
        self.counter = DiskCounter()
        self._lock = threading.RLock()
        self.catalog = Catalog(self.data_dir, self.counter)
        self.tables: dict[str, Table] = {
            name: Table(info, self.data_dir, self.counter) for name, info in self.catalog.tables.items()
        }

    # ------------------------------------------------------------------ SQL
    def execute(self, sql: str, page: int = 1, page_size: int = 50, planner: str | None = None) -> dict[str, Any]:
        """Ejecuta un script; la respuesta corresponde a la última sentencia."""
        mode = planner or self.planner_mode
        if mode not in PLANNER_MODES:
            raise SemanticError(f"planner debe ser 'rules' o 'cost' (se recibió '{mode}')")
        if page < 1 or page_size < 1:
            raise SemanticError("page y page_size deben ser >= 1")
        with self._lock:
            results: list[StatementResult] = []
            statements = Parser(sql).statements()
            while True:
                t0 = time.perf_counter()
                try:
                    stmt = next(statements)
                except StopIteration:
                    break
                except Exception as exc:
                    exc.executed = [r.summary() for r in results]  # type: ignore[attr-defined]
                    raise
                parse_s = time.perf_counter() - t0
                self.counter.reset()
                t1 = time.perf_counter()
                try:
                    result = execute_statement(self, stmt, page, page_size, mode)
                except Exception as exc:
                    self._end_of_statement()
                    exc.executed = [r.summary() for r in results]  # type: ignore[attr-defined]
                    raise
                self._end_of_statement()
                exec_s = time.perf_counter() - t1
                result.metrics = {
                    **self.counter.as_dict(),
                    "parse_ms": _ms(parse_s),
                    "exec_ms": _ms(exec_s),
                    "total_ms": _ms(parse_s + exec_s),
                }
                results.append(result)
            if not results:
                raise SemanticError("no hay sentencias para ejecutar")
            last = results[-1]
            return {
                "statement": last.statement,
                "columns": last.columns,
                "rows": last.rows,
                "total_rows": last.total_rows,
                "page": page,
                "page_size": page_size,
                "message": last.message,
                "plan": last.plan,
                "metrics": last.metrics,
                "statements": [r.summary() for r in results],
            }

    def _end_of_statement(self) -> None:
        """Persiste las páginas 0 con cambios estructurales (cuentan como escrituras)."""
        for table in self.tables.values():
            table.flush()

    # ------------------------------------------------------------------ tablas
    def get_table(self, name: str) -> Table:
        table = self.tables.get(name)
        if table is None:
            raise SemanticError(f"no existe la tabla '{name}'")
        return table

    def tables_info(self) -> list[dict[str, Any]]:
        with self._lock:
            return [t.stats() for t in self.tables.values()]

    def reorganize(self, name: str) -> dict[str, Any]:
        with self._lock:
            table = self.get_table(name)
            if not isinstance(table.file, SequentialFile):
                raise SemanticError(f"'{name}' es HEAP: solo las tablas SEQUENTIAL se reorganizan")
            self.counter.reset()
            t0 = time.perf_counter()
            stats = table.reorganize()
            self._end_of_statement()
            elapsed = time.perf_counter() - t0
            return {
                "table": name,
                "message": (
                    f"Reorganización completa: {stats.main_pages_before} → {stats.main_pages_after} páginas "
                    f"principales, {stats.overflow_pages_before} páginas de overflow vaciadas"
                    + (f", {len(table.indexes)} índice(s) reconstruido(s)" if table.indexes else "")
                ),
                "before": {
                    "main_pages": stats.main_pages_before,
                    "overflow_pages": stats.overflow_pages_before,
                    "record_count": stats.records,
                },
                "after": {"main_pages": stats.main_pages_after, "overflow_pages": 0, "record_count": stats.records},
                "metrics": {
                    **self.counter.as_dict(),
                    "parse_ms": 0.0,
                    "exec_ms": _ms(elapsed),
                    "total_ms": _ms(elapsed),
                },
            }

    def table_files(self, name: str) -> list[dict[str, Any]]:
        """Archivos binarios de una tabla (para el inspector de páginas)."""
        with self._lock:
            table = self.get_table(name)
            out = []
            kinds = ["HEAP"] if not table.is_sequential else ["SEQUENTIAL", "OVERFLOW"]
            keys = ["data"] if not table.is_sequential else ["data", "overflow"]
            for key, kind, fname in zip(keys, kinds, table.info.files):
                path = self.data_dir / fname
                out.append({"key": key, "kind": kind, "path": fname,
                            "num_pages": path.stat().st_size // table.info.page_size,
                            "page_size": table.info.page_size})
            for idx in table.indexes.values():
                out.append({"key": idx.name, "kind": idx.kind.value, "path": idx.info.file,
                            "num_pages": idx.path.stat().st_size // table.info.page_size,
                            "page_size": table.info.page_size})
            return out

    def file_path(self, name: str, key: str) -> tuple[Table, Path, str]:
        table = self.get_table(name)
        if key == "data":
            return table, self.data_dir / table.info.files[0], "data"
        if key == "overflow" and table.is_sequential:
            return table, self.data_dir / table.info.files[1], "overflow"
        idx = table.indexes.get(key)
        if idx is None:
            raise SemanticError(f"'{key}' no es un archivo de '{name}' (use data, overflow o el nombre de un índice)")
        return table, idx.path, key

    # ------------------------------------------------------------------ ciclo de vida
    def sync(self) -> None:
        with self._lock:
            for table in self.tables.values():
                table.sync()

    def close(self) -> None:
        with self._lock:
            for table in self.tables.values():
                table.close()
            self.tables.clear()
