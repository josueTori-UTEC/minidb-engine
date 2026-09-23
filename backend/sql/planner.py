"""Planificador: elige la ruta de acceso para SELECT/DELETE y estima su costo en I/O.

Caminos candidatos (generados según las reglas del enunciado):

1. **Igualdad** ``col = v``:
   * ``IndexScan`` si hay un índice HASH o BTREE sobre ``col``;
   * ``BinarySearch`` si la tabla es SEQUENTIAL y ``col`` es su PK.
2. **Rango** (``<``, ``<=``, ``>``, ``>=``, ``BETWEEN`` o dos cotas con ``AND``):
   * ``IndexRangeScan`` si hay un BTREE sobre ``col``;
   * ``SeqFileRangeScan`` si la tabla es SEQUENTIAL y ``col`` es su PK.
3. ``SeqScan`` en cualquier otro caso.

Modo ``rules`` (por defecto, el del enunciado): se toma el primer grupo que
tenga candidatos (igualdad, luego rango, luego SeqScan) y dentro del grupo el de
menor costo estimado. Modo ``cost``: el de menor costo entre todos (incluido
SeqScan), como haría un optimizador basado en costos. Los predicados que no usa
el acceso se aplican como filtro residual.

Costos estimados (lecturas; ``P`` páginas, ``h`` altura del B+, ``M`` páginas
principales del Sequential, ``k`` registros que califican):

* SeqScan = P
* IndexScan BTREE = h + k (k = 1 si el índice es único)
* IndexScan HASH = 2 + k (directorio + bucket)
* IndexRangeScan = h + (hojas - 1) + k
* BinarySearch = ceil(log2 M) + overflow promedio por página
* SeqFileRangeScan = ceil(log2 M) + páginas del rango (+ su overflow)

``k`` se estima con la selectividad: 1 para igualdad sobre columna única; 1/10
para igualdad sobre columna no única; para rangos numéricos se interpola con el
mínimo y máximo de la clave (estadística guardada en el índice o el Sequential);
sin estadísticas se asume 1/4.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Sequence

from backend.files.sequential_file import SequentialFile
from backend.indexes.bplus_tree import BPlusTree
from backend.sql.predicates import BoundPredicate, ColumnBounds, range_bounds
from backend.storage.catalog import IndexKind
from backend.storage.record import ColumnType
from backend.table import Index, Table

PLANNER_MODES = ("rules", "cost")
DEFAULT_EQ_SELECTIVITY = 0.1
DEFAULT_RANGE_SELECTIVITY = 0.25

GROUP_EQUALITY, GROUP_RANGE, GROUP_SCAN = 1, 2, 3


@dataclass
class AccessPath:
    type: str
    structure: str
    group: int
    estimated_io: int
    detail: str
    index: Index | None = None
    column: str | None = None
    predicate: str | None = None
    eq_value: Any = None
    bounds: ColumnBounds | None = None
    used: list[BoundPredicate] = field(default_factory=list)
    estimated_rows: float = 0.0

    @property
    def index_name(self) -> str | None:
        return None if self.index is None else self.index.name

    def summary(self, chosen: bool) -> dict[str, Any]:
        return {
            "type": self.type,
            "structure": self.structure,
            "index": self.index_name,
            "column": self.column,
            "estimated_io": self.estimated_io,
            "chosen": chosen,
        }


@dataclass
class Plan:
    table: str
    access: AccessPath
    residual: list[BoundPredicate]
    candidates: list[AccessPath]
    mode: str

    def as_dict(self) -> dict[str, Any]:
        a = self.access
        return {
            "type": a.type,
            "table": self.table,
            "structure": a.structure,
            "index": a.index_name,
            "column": a.column,
            "predicate": a.predicate,
            "filter": " AND ".join(str(p) for p in self.residual) or None,
            "estimated_io": a.estimated_io,
            "estimated_rows": round(a.estimated_rows, 1),
            "detail": a.detail,
            "planner": self.mode,
            "candidates": [c.summary(c is a) for c in self.candidates],
        }


# ---------------------------------------------------------------------- estimaciones
def _range_selectivity(bounds: ColumnBounds, lo_stat: float | None, hi_stat: float | None, numeric: bool) -> float:
    if not numeric or lo_stat is None or hi_stat is None:
        return DEFAULT_RANGE_SELECTIVITY
    span = hi_stat - lo_stat
    if span <= 0:
        return 1.0
    lo = lo_stat if bounds.low is None else max(float(bounds.low), lo_stat)
    hi = hi_stat if bounds.high is None else min(float(bounds.high), hi_stat)
    if hi < lo:
        return 0.0
    # +1 porque para claves enteras un rango [a, b] contiene b - a + 1 valores
    return min(1.0, (hi - lo + 1) / (span + 1))


def _log2_pages(m: int) -> int:
    return max(1, math.ceil(math.log2(m))) if m > 1 else 1


def _seqscan_pages(table: Table) -> int:
    f = table.file
    if isinstance(f, SequentialFile):
        return f.main_pages + f.overflow_pages
    return f.page_count


def _eq_rows(table: Table, index_unique: bool) -> float:
    n = table.file.record_count
    return 1.0 if index_unique else max(1.0, n * DEFAULT_EQ_SELECTIVITY)


def _index_scan(table: Table, index: Index, p: BoundPredicate) -> AccessPath:
    rows = _eq_rows(table, index.unique)
    if isinstance(index.structure, BPlusTree):
        h = max(1, index.structure.height)
        per_leaf = max(1.0, index.structure.entry_count / max(1, index.structure.leaf_pages))
        extra_leaves = 0 if index.unique else max(0, math.ceil(rows / per_leaf) - 1)
        cost = h + extra_leaves + math.ceil(rows)
        detail = f"B+ de altura {h}: {h} lecturas hasta la hoja + 1 por registro traído por RID"
        structure = "BTREE"
    else:
        chain = 0 if index.unique else max(0, math.ceil(rows / index.structure.bucket_capacity) - 1)
        cost = 2 + chain + math.ceil(rows)
        detail = "Hash extensible: 1 lectura de directorio + 1 de bucket (+ overflow) + 1 por registro"
        structure = "HASH"
    return AccessPath(
        "IndexScan", structure, GROUP_EQUALITY, cost, detail, index=index, column=p.column.name,
        predicate=str(p), eq_value=p.value, used=[p], estimated_rows=rows,
    )


def _binary_search(table: Table, p: BoundPredicate) -> AccessPath:
    f = table.file
    assert isinstance(f, SequentialFile)
    m = f.main_pages
    chain = f.overflow_pages / m if m else 0.0
    cost = _log2_pages(m) + math.ceil(chain)
    return AccessPath(
        "BinarySearch", "SEQUENTIAL", GROUP_EQUALITY, cost,
        f"Búsqueda binaria sobre {m} páginas principales (~log2 M = {_log2_pages(m)}) + overflow de la página",
        column=p.column.name, predicate=str(p), eq_value=p.value, used=[p], estimated_rows=1.0,
    )


def _index_range_scan(table: Table, index: Index, bounds: ColumnBounds) -> AccessPath:
    tree = index.structure
    assert isinstance(tree, BPlusTree)
    numeric = index.column.type != ColumnType.CHAR
    sel = _range_selectivity(bounds, tree.min_key, tree.max_key, numeric)
    rows = sel * tree.entry_count
    per_leaf = max(1.0, tree.entry_count / max(1, tree.leaf_pages))
    leaves = max(1, math.ceil(rows / per_leaf))
    h = max(1, tree.height)
    cost = h + leaves - 1 + math.ceil(rows)
    return AccessPath(
        "IndexRangeScan", "BTREE", GROUP_RANGE, cost,
        f"B+ de altura {h}: bajar a la primera hoja, recorrer ~{leaves} hoja(s) por next_leaf y traer "
        f"~{math.ceil(rows)} registro(s) por RID (selectividad estimada {sel:.2%})",
        index=index, column=index.column.name, predicate=bounds.describe(index.column.name),
        bounds=bounds, used=list(bounds.predicates or []), estimated_rows=rows,
    )


def _seq_range_scan(table: Table, bounds: ColumnBounds) -> AccessPath:
    f = table.file
    assert isinstance(f, SequentialFile)
    col = table.schema.columns[f.key_index]
    sel = _range_selectivity(bounds, f.min_key, f.max_key, col.type != ColumnType.CHAR)
    m, o = f.main_pages, f.overflow_pages
    pages = math.ceil(sel * m) + math.ceil(sel * o)
    start = _log2_pages(m) if bounds.low is not None else 0
    cost = start + max(1, pages)
    return AccessPath(
        "SeqFileRangeScan", "SEQUENTIAL", GROUP_RANGE, cost,
        f"Búsqueda binaria de la cota inferior + ~{max(1, pages)} página(s) contiguas del rango "
        f"(selectividad estimada {sel:.2%})",
        column=col.name, predicate=bounds.describe(col.name), bounds=bounds,
        used=list(bounds.predicates or []), estimated_rows=sel * f.record_count,
    )


def _seq_scan(table: Table) -> AccessPath:
    p = _seqscan_pages(table)
    what = "principales + overflow" if table.is_sequential else "de datos"
    return AccessPath(
        "SeqScan", table.organization.value, GROUP_SCAN, p,
        f"Full scan: lee las {p} páginas {what}", estimated_rows=float(table.file.record_count),
    )


# ---------------------------------------------------------------------- planner
def plan(table: Table, predicates: Sequence[BoundPredicate], mode: str = "rules") -> Plan:
    if mode not in PLANNER_MODES:
        raise ValueError(f"modo de planner inválido: {mode}")
    candidates: list[AccessPath] = []
    pk = table.schema.pk_index
    seen_eq: set[int] = set()
    for p in predicates:
        if p.op != "=" or p.col_index in seen_eq:
            continue
        seen_eq.add(p.col_index)
        for idx in table.indexes_on(p.col_index):
            candidates.append(_index_scan(table, idx, p))
        if table.is_sequential and p.col_index == pk:
            candidates.append(_binary_search(table, p))
    for col_index, bounds in range_bounds(predicates).items():
        for idx in table.indexes_on(col_index):
            if idx.kind == IndexKind.BTREE:
                candidates.append(_index_range_scan(table, idx, bounds))
        if table.is_sequential and col_index == pk:
            candidates.append(_seq_range_scan(table, bounds))
    candidates.append(_seq_scan(table))

    if mode == "rules":
        first_group = min(c.group for c in candidates)
        pool = [c for c in candidates if c.group == first_group]
    else:
        pool = candidates
    chosen = min(pool, key=lambda c: (c.estimated_io, c.group))
    used = {id(p) for p in chosen.used}
    residual = [p for p in predicates if id(p) not in used]
    return Plan(table.name, chosen, residual, candidates, mode)
