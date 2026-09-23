"""Ejecución de sentencias ya parseadas sobre la base de datos.

Cada función recibe la ``Database`` (catálogo, tablas abiertas y configuración)
y devuelve un ``StatementResult``. Las métricas de I/O y tiempo las agrega
``Database.execute`` alrededor de cada sentencia.
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator

from backend.files.common import DuplicateKeyError, RecordNotFoundError
from backend.files.sequential_file import SequentialFile
from backend.indexes.bplus_tree import BPlusTree
from backend.sql import ast
from backend.sql.errors import SemanticError
from backend.sql.planner import Plan, _log2_pages
from backend.sql.planner import plan as make_plan
from backend.sql.predicates import BoundPredicate, bind, compile_raw_filter, matches_all
from backend.storage.catalog import CatalogError, IndexInfo, IndexKind, Organization, TableInfo
from backend.storage.page import PageFormatError
from backend.storage.record import Column, ColumnType, Schema, SchemaError
from backend.table import Table, index_file, table_files

if TYPE_CHECKING:
    from backend.database import Database


@dataclass
class StatementResult:
    statement: str
    sql: str
    columns: list[str] = field(default_factory=list)
    rows: list[list[Any]] = field(default_factory=list)
    total_rows: int = 0
    message: str = ""
    plan: dict[str, Any] | None = None
    metrics: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        return {"sql": self.sql, "statement": self.statement, "message": self.message, "metrics": self.metrics}


def execute_statement(
    db: Database, stmt: ast.Statement, page: int, page_size: int, mode: str
) -> StatementResult:
    handler = _HANDLERS.get(type(stmt))
    if handler is None:
        raise SemanticError(f"sentencia no soportada: {stmt.kind}")
    try:
        return handler(db, stmt, page, page_size, mode)
    except SemanticError:
        raise
    except DuplicateKeyError as exc:
        raise SemanticError(str(exc)) from None
    except (CatalogError, SchemaError, RecordNotFoundError, PageFormatError) as exc:
        raise SemanticError(str(exc)) from None


# ---------------------------------------------------------------------- utilidades
def _table(db: Database, name: str) -> Table:
    table = db.tables.get(name)
    if table is None:
        raise SemanticError(f"no existe la tabla '{name}'")
    return table


def _rows_for_plan(table: Table, plan: Plan) -> Iterator[tuple[Any, tuple[Any, ...]]]:
    """Ejecuta el camino de acceso del plan y aplica el filtro residual.

    Produce ``(rid, valores)`` de los registros que cumplen todo el WHERE.
    """
    access = plan.access
    residual = plan.residual
    file = table.file
    if access.type == "SeqScan":
        yield from file.scan(compile_raw_filter(table.schema, residual))
        return
    if access.type == "BinarySearch":
        assert isinstance(file, SequentialFile)
        for rid, values in file.find(access.eq_value):
            if matches_all(residual, values):
                yield rid, values
        return
    if access.type == "SeqFileRangeScan":
        assert isinstance(file, SequentialFile) and access.bounds is not None
        b = access.bounds
        yield from file.range_scan(
            b.low, b.high, low_inclusive=b.low_inclusive, high_inclusive=b.high_inclusive,
            raw_filter=compile_raw_filter(table.schema, residual),
        )
        return
    index = access.index
    assert index is not None
    if access.type == "IndexScan":
        rids: Iterator[Any] = iter(index.search(access.eq_value))
    else:  # IndexRangeScan
        b = access.bounds
        assert b is not None
        rids = index.range(b.low, b.high, b.low_inclusive, b.high_inclusive)
    for rid in rids:
        values = table.fetch(rid)  # 1 lectura por registro (índice no agrupado)
        if matches_all(residual, values):
            yield rid, values


def _plan_for(db: Database, table: Table, where: list[ast.Predicate], mode: str) -> tuple[Plan, list[BoundPredicate]]:
    bound = bind(table.schema, where)
    return make_plan(table, bound, mode), bound


def _insert_cost(table: Table) -> int:
    """Costo estimado de insertar una fila (archivo + índices)."""
    f = table.file
    cost = (_log2_pages(f.main_pages) + 1) if isinstance(f, SequentialFile) else 2
    for idx in table.indexes.values():
        s = idx.structure
        cost += (max(1, s.height) + 1) if isinstance(s, BPlusTree) else 3
    return cost


def _index_maintenance_cost(table: Table) -> int:
    total = 0
    for idx in table.indexes.values():
        s = idx.structure
        total += (max(1, s.height) + 1) if isinstance(s, BPlusTree) else 3
    return total


# ---------------------------------------------------------------------- SELECT / DELETE / EXPLAIN
def _select(db: Database, stmt: ast.Select, page: int, page_size: int, mode: str) -> StatementResult:
    table = _table(db, stmt.table)
    schema = table.schema
    if stmt.columns is None:
        positions = list(range(len(schema)))
    else:
        for c in stmt.columns:
            if not schema.has_column(c):
                raise SemanticError(f"no existe la columna '{c}' en '{table.name}'")
        positions = [schema.index_of(c) for c in stmt.columns]
    plan, _ = _plan_for(db, table, stmt.where, mode)
    start = (page - 1) * page_size
    end = start + page_size
    rows: list[list[Any]] = []
    total = 0
    for _, values in _rows_for_plan(table, plan):
        if start <= total < end:
            rows.append([values[i] for i in positions])
        total += 1
    return StatementResult(
        "SELECT", stmt.sql, [schema.names[i] for i in positions], rows, total, f"{total} fila(s)", plan.as_dict()
    )


def _delete(db: Database, stmt: ast.Delete, page: int, page_size: int, mode: str) -> StatementResult:
    table = _table(db, stmt.table)
    plan, _ = _plan_for(db, table, stmt.where, mode)
    if plan.access.type == "SeqScan":
        count = table.delete_where(compile_raw_filter(table.schema, plan.residual))
    else:
        rids = [rid for rid, _ in _rows_for_plan(table, plan)]
        count = len(table.delete_rids(rids))
    info = plan.as_dict()
    rows_est = math.ceil(plan.access.estimated_rows)
    # lectura para ubicar + escritura de la página (+ relectura si se llegó por índice) + índices
    per_row = 1 + (0 if plan.access.type == "SeqScan" else 1) + _index_maintenance_cost(table)
    info["estimated_io"] = plan.access.estimated_io + rows_est * per_row
    info["detail"] = f"{plan.access.detail}; luego borrado lógico y mantenimiento de {len(table.indexes)} índice(s)"
    return StatementResult("DELETE", stmt.sql, message=f"{count} fila(s) eliminada(s)", plan=info, total_rows=0)


def _explain(db: Database, stmt: ast.Explain, page: int, page_size: int, mode: str) -> StatementResult:
    inner = stmt.statement
    table = _table(db, inner.table)
    plan, _ = _plan_for(db, table, inner.where, mode)
    info = plan.as_dict()
    rows = [
        [c["type"], c["structure"], c["index"] or "", c["estimated_io"], "sí" if c["chosen"] else ""]
        for c in info["candidates"]
    ]
    return StatementResult(
        "EXPLAIN",
        stmt.sql,
        ["camino", "estructura", "índice", "I/O estimado", "elegido"],
        rows,
        len(rows),
        f"Plan elegido: {info['type']} (costo estimado {info['estimated_io']} I/O; no se ejecutó la consulta)",
        info,
    )


# ---------------------------------------------------------------------- INSERT / COPY
def _insert_rows(table: Table, rows: Iterator[list[Any]]) -> tuple[int, int]:
    """Inserta filas validando tipos; devuelve (filas insertadas, reorganizaciones)."""
    count = reorgs = 0
    for values in rows:
        try:
            coerced = table.schema.coerce(values)
        except SchemaError as exc:
            raise SemanticError(f"fila {count + 1}: {exc}") from None
        try:
            _, reorganized = table.insert(coerced)
        except DuplicateKeyError as exc:
            raise SemanticError(f"{exc} (se insertaron {count} fila(s) antes del error)") from None
        reorgs += reorganized
        count += 1
    return count, reorgs


def _insert_plan(table: Table, rows: int | None) -> dict[str, Any]:
    per_row = _insert_cost(table)
    parts = ["Heap: 1R + 1W (free-list)" if table.organization == Organization.HEAP else
             "Sequential: búsqueda binaria + 1W"]
    for idx in table.indexes.values():
        s = idx.structure
        parts.append(f"{idx.name}: {s.height}R + 1W" if isinstance(s, BPlusTree) else f"{idx.name}: 2R + 1W")
    return {
        "type": "Insert",
        "table": table.name,
        "structure": table.organization.value,
        "index": None,
        "column": None,
        "predicate": None,
        "filter": None,
        "estimated_io": None if rows is None else per_row * rows,
        "estimated_rows": rows,
        "detail": "Por fila: " + "; ".join(parts),
        "planner": None,
        "candidates": [],
    }


def _insert(db: Database, stmt: ast.Insert, page: int, page_size: int, mode: str) -> StatementResult:
    table = _table(db, stmt.table)
    plan = _insert_plan(table, len(stmt.rows))
    count, reorgs = _insert_rows(table, iter(stmt.rows))
    msg = f"{count} fila(s) insertada(s)"
    if reorgs:
        msg += f" · {reorgs} reorganización(es) automática(s) del Sequential File"
    return StatementResult("INSERT", stmt.sql, message=msg, plan=plan)


def _resolve_data_path(db: Database, raw: str) -> Path:
    base = db.data_dir.resolve()
    path = (base / raw).resolve()
    if base not in path.parents and path != base:
        raise SemanticError("COPY solo puede leer archivos dentro de la carpeta de datos (MINIDB_DATA_DIR)")
    if not path.is_file():
        raise SemanticError(f"no existe el archivo '{raw}' en la carpeta de datos")
    return path


def _csv_rows(path: Path, schema: Schema) -> Iterator[list[Any]]:
    converters = []
    for col in schema.columns:
        if col.type == ColumnType.INT:
            converters.append(int)
        elif col.type == ColumnType.FLOAT:
            converters.append(float)
        else:
            converters.append(str)
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        for line_no, fields in enumerate(reader, start=1):
            if not fields:
                continue
            if line_no == 1 and [f.strip().lower() for f in fields] == list(schema.names):
                continue  # encabezado
            if len(fields) != len(converters):
                raise SemanticError(f"línea {line_no} del CSV: {len(fields)} campos (se esperaban {len(converters)})")
            try:
                yield [conv(f.strip() if conv is not str else f) for conv, f in zip(converters, fields)]
            except ValueError as exc:
                raise SemanticError(f"línea {line_no} del CSV: {exc}") from None


def _copy(db: Database, stmt: ast.Copy, page: int, page_size: int, mode: str) -> StatementResult:
    table = _table(db, stmt.table)
    path = _resolve_data_path(db, stmt.path)
    plan = _insert_plan(table, None)
    count, reorgs = _insert_rows(table, _csv_rows(path, table.schema))
    plan["estimated_io"] = _insert_cost(table) * count
    plan["estimated_rows"] = count
    msg = f"{count} fila(s) cargada(s) desde '{stmt.path}'"
    if reorgs:
        msg += f" · {reorgs} reorganización(es) automática(s)"
    return StatementResult("COPY", stmt.sql, message=msg, plan=plan)


# ---------------------------------------------------------------------- DDL
def _create_table(db: Database, stmt: ast.CreateTable, page: int, page_size: int, mode: str) -> StatementResult:
    if stmt.name in db.tables:
        raise SemanticError(f"la tabla '{stmt.name}' ya existe")
    try:
        columns = [Column(c.name, c.type, c.size, c.primary_key) for c in stmt.columns]
        schema = Schema(columns)
    except SchemaError as exc:
        raise SemanticError(str(exc)) from None
    if stmt.organization == Organization.SEQUENTIAL and schema.pk_index is None:
        raise SemanticError("una tabla SEQUENTIAL necesita una columna PRIMARY KEY (clave de ordenamiento)")
    info = TableInfo(
        name=stmt.name,
        columns=columns,
        organization=stmt.organization,
        page_size=db.page_size,
        files=table_files(stmt.name, stmt.organization),
        fill_factor=db.fill_factor,
        reorg_ratio=db.reorg_ratio,
        auto_reorganize=True,
    )
    table = Table(info, db.data_dir, db.counter, create=True)
    db.catalog.add_table(info)
    db.tables[stmt.name] = table
    return StatementResult(
        "CREATE TABLE",
        stmt.sql,
        message=(
            f"Tabla '{stmt.name}' creada ({stmt.organization.value}, registro de {schema.record_size} B, "
            f"{table.file.capacity} registros por página de {db.page_size} B)"
        ),
    )


def _create_index(db: Database, stmt: ast.CreateIndex, page: int, page_size: int, mode: str) -> StatementResult:
    table = _table(db, stmt.table)
    if db.catalog.index_exists(stmt.name):
        raise SemanticError(f"el índice '{stmt.name}' ya existe")
    if not table.schema.has_column(stmt.column):
        raise SemanticError(f"no existe la columna '{stmt.column}' en '{table.name}'")
    col_index = table.schema.index_of(stmt.column)
    if any(i.kind == stmt.method for i in table.indexes_on(col_index)):
        raise SemanticError(f"ya existe un índice {stmt.method.value} sobre {table.name}.{stmt.column}")
    info = IndexInfo(stmt.name, table.name, stmt.column, stmt.method, index_file(table.name, stmt.column, stmt.method))
    try:
        index = table.add_index(info)
    except DuplicateKeyError as exc:
        raise SemanticError(f"no se puede crear el índice: la PK tiene valores repetidos ({exc})") from None
    db.catalog.add_index(info)
    s = index.stats()
    shape = f"altura {s['height']}" if stmt.method == IndexKind.BTREE else f"profundidad global {s['global_depth']}"
    return StatementResult(
        "CREATE INDEX",
        stmt.sql,
        message=(
            f"Índice '{stmt.name}' ({stmt.method.value}{', único' if index.unique else ''}) creado sobre "
            f"{table.name}.{stmt.column}: {s['entry_count']} entradas, {shape}, {s['page_count']} páginas"
        ),
    )


def _drop_table(db: Database, stmt: ast.DropTable, page: int, page_size: int, mode: str) -> StatementResult:
    table = _table(db, stmt.name)
    db.catalog.drop_table(stmt.name)
    del db.tables[stmt.name]
    table.drop_files()
    return StatementResult("DROP TABLE", stmt.sql, message=f"Tabla '{stmt.name}' eliminada")


def _drop_index(db: Database, stmt: ast.DropIndex, page: int, page_size: int, mode: str) -> StatementResult:
    info, idx = db.catalog.find_index(stmt.name)
    db.catalog.drop_index(stmt.name)
    db.tables[info.name].remove_index(idx.name)
    return StatementResult("DROP INDEX", stmt.sql, message=f"Índice '{stmt.name}' eliminado")


_HANDLERS: dict[type, Any] = {
    ast.Select: _select,
    ast.Delete: _delete,
    ast.Explain: _explain,
    ast.Insert: _insert,
    ast.Copy: _copy,
    ast.CreateTable: _create_table,
    ast.CreateIndex: _create_index,
    ast.DropTable: _drop_table,
    ast.DropIndex: _drop_index,
}
