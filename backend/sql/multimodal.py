"""Planificación y ejecución de consultas multimodales (espacial, textual y vectorial).

Atiende los ``SELECT`` que usan algo más que comparaciones sobre columnas:

* predicados ``WITHIN_RADIUS`` / ``WITHIN_BOX`` (R-Tree) y ``MATCH`` (índice invertido);
* expresiones ``COSINE_SCORE()``, ``col <-> operando`` y ``col <=> operando``;
* ``ORDER BY ... LIMIT k`` (Top-K).

**Estrategias.** Cuando el ``ORDER BY`` puede resolverse con un índice de
ranking (IVF para vectores, R-Tree para k-NN geográfico, índice invertido para
relevancia) hay dos formas de combinarlo con los demás filtros del ``WHERE``:

*Post-filtering*
    El índice de ranking entrega candidatos en orden; se trae cada registro y se
    le aplican los demás predicados hasta completar ``k``. Barato cuando el
    filtro deja pasar a casi todos; con un IVF puede devolver menos de ``k``
    filas si el filtro es muy selectivo (solo se ven las celdas sondeadas).

*Pre-filtering*
    Primero se resuelven los filtros (con B+, hash, R-Tree, índice invertido o
    full scan) y el ranking se calcula de forma exacta sobre las filas que
    quedan (Top-K con un *heap*). Para el texto, los RID del índice relacional se
    pasan como conjunto permitido al índice invertido.

El planificador estima el I/O de ambas y elige la menor (``SET hybrid.strategy =
pre | post | auto`` permite forzarla). El plan informa la estrategia, el camino
de acceso y los candidatos descartados; el desglose de bloques leídos en el
índice y en la tabla lo agrega ``Database.execute`` a las métricas.

Sin índice aplicable todo se resuelve con un recorrido secuencial que evalúa los
predicados fila por fila (distancia de Haversine, coincidencia de términos,
distancia exacta al vector): es la línea base de los experimentos.
"""

from __future__ import annotations

import heapq
import itertools
import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Iterator, Sequence

import numpy as np

from backend.indexes.inverted_index import InvertedIndex, InvertedIndexError, compile_match
from backend.indexes.ivf import IVFIndex, distances
from backend.indexes.rtree import RTree, SearchTrace, geo_weights, haversine_km, KM_PER_DEG_LAT
from backend.sql import ast
from backend.sql.errors import SemanticError
from backend.sql.planner import DEFAULT_EQ_SELECTIVITY, DEFAULT_RANGE_SELECTIVITY, Plan, _range_selectivity
from backend.sql.planner import plan as make_plan
from backend.sql.predicates import BoundPredicate, bind, matches_all, range_bounds
from backend.indexes.bplus_tree import BPlusTree
from backend.storage.catalog import IndexKind
from backend.storage.record import ColumnType, SchemaError, parse_point, parse_vector
from backend.storage.rid import RID
from backend.table import Index, Table
from backend.text.analyzer import Analyzer

if TYPE_CHECKING:
    from backend.database import Database

DEFAULT_SPATIAL_SELECTIVITY = 0.05
DEFAULT_TEXT_SELECTIVITY = 0.05
_METRIC_BY_OP = {"<->": "L2", "<=>": "COSINE"}

Row = tuple[Any, tuple[Any, ...], dict[Any, float]]  # (rid, valores, expresiones calculadas)


# ---------------------------------------------------------------------- consulta resuelta
@dataclass
class Output:
    name: str
    column: int | None = None  # columna de la tabla ...
    key: Any = None  # ... o clave de una expresión calculada


@dataclass
class Query:
    table: Table
    scalar: list[BoundPredicate]
    spatial: ast.WithinRadius | ast.WithinBox | None
    spatial_col: int | None
    match: ast.Match | None
    match_col: int | None
    outputs: list[Output]
    order_key: Any = None  # clave de expresión o ('column', i)
    descending: bool = False
    limit: int | None = None
    needs: set[Any] = field(default_factory=set)  # expresiones calculadas que hay que producir

    @property
    def needs_score(self) -> bool:
        return ("score",) in self.needs

    def index_for(self, col: int | None, kind: IndexKind) -> Index | None:
        if col is None:
            return None
        return next((i for i in self.table.indexes_on(col) if i.kind == kind), None)

    @property
    def has_filters(self) -> bool:
        return bool(self.scalar) or self.spatial is not None or self.match is not None


def _expr_key(table: Table, expr: ast.Expr, has_match: bool) -> Any:
    schema = table.schema
    if expr.kind == "score":
        if not has_match:
            raise SemanticError("COSINE_SCORE() necesita un predicado MATCH en el WHERE")
        return ("score",)
    if expr.kind == "distance":
        if not schema.has_column(expr.column or ""):
            raise SemanticError(f"no existe la columna '{expr.column}' en '{table.name}'")
        i = schema.index_of(expr.column or "")
        col = schema.columns[i]
        try:
            if col.type == ColumnType.POINT:
                if expr.op != "<->":
                    raise SemanticError("sobre una columna POINT solo se admite el operador <-> (distancia en km)")
                return ("gdist", i, parse_point(expr.operand))
            if col.type == ColumnType.VECTOR:
                return ("vdist", i, _METRIC_BY_OP[expr.op or "<->"], parse_vector(expr.operand, col.size))
        except SchemaError as exc:
            raise SemanticError(str(exc)) from None
        raise SemanticError(
            f"los operadores de distancia <-> y <=> se aplican a columnas POINT o VECTOR; "
            f"'{col.name}' es {col.display_type()}"
        )
    raise AssertionError(expr.kind)


def resolve(table: Table, stmt: ast.Select) -> Query:
    """Liga la sentencia al esquema: valida columnas, tipos y expresiones."""
    schema = table.schema
    scalar_src = [p for p in stmt.where if isinstance(p, ast.SCALAR_PREDICATES)]
    spatial = [p for p in stmt.where if isinstance(p, (ast.WithinRadius, ast.WithinBox))]
    match = [p for p in stmt.where if isinstance(p, ast.Match)]
    if len(spatial) > 1:
        raise SemanticError("solo se admite un predicado espacial (WITHIN_RADIUS o WITHIN_BOX) por consulta")
    if len(match) > 1:
        raise SemanticError("solo se admite un predicado MATCH por consulta")
    spatial_col = match_col = None
    if spatial:
        name = spatial[0].column
        if not schema.has_column(name):
            raise SemanticError(f"no existe la columna '{name}' en '{table.name}'")
        spatial_col = schema.index_of(name)
        if schema.columns[spatial_col].type != ColumnType.POINT:
            raise SemanticError(f"WITHIN_RADIUS / WITHIN_BOX necesitan una columna POINT ('{name}' no lo es)")
    if match:
        name = match[0].column
        if not schema.has_column(name):
            raise SemanticError(f"no existe la columna '{name}' en '{table.name}'")
        match_col = schema.index_of(name)
        if schema.columns[match_col].type != ColumnType.CHAR:
            raise SemanticError(f"MATCH necesita una columna de texto ('{name}' no lo es)")
    q = Query(
        table, bind(schema, scalar_src), spatial[0] if spatial else None, spatial_col,
        match[0] if match else None, match_col, [], limit=stmt.limit,
    )
    aliases: dict[str, Any] = {}
    if stmt.items is None:
        names = list(schema.names) if stmt.columns is None else stmt.columns
        for name in names:
            if not schema.has_column(name):
                raise SemanticError(f"no existe la columna '{name}' en '{table.name}'")
            q.outputs.append(Output(name, column=schema.index_of(name)))
    else:
        for item in stmt.items:
            if item.expr.kind == "column":
                name = item.expr.column or ""
                if not schema.has_column(name):
                    raise SemanticError(f"no existe la columna '{name}' en '{table.name}'")
                q.outputs.append(Output(item.alias or name, column=schema.index_of(name)))
                if item.alias:
                    aliases[item.alias] = ("column", schema.index_of(name))
            else:
                key = _expr_key(table, item.expr, bool(match))
                q.outputs.append(Output(item.name, key=key))
                q.needs.add(key)
                if item.alias:
                    aliases[item.alias] = key
    if stmt.order_by is not None:
        expr = stmt.order_by.expr
        if expr.kind == "column":
            name = expr.column or ""
            if name in aliases:
                key = aliases[name]
            elif schema.has_column(name):
                col = schema.columns[schema.index_of(name)]
                if not col.is_scalar:
                    raise SemanticError(f"no se puede ordenar por la columna {col.display_type()} '{name}'")
                key = ("column", schema.index_of(name))
            else:
                raise SemanticError(f"ORDER BY: '{name}' no es una columna ni un alias de la consulta")
        else:
            key = _expr_key(table, expr, bool(match))
        q.order_key = key
        q.descending = stmt.order_by.descending
        if key[0] != "column":
            q.needs.add(key)
    if q.limit is not None and q.limit < 0:
        raise SemanticError("LIMIT debe ser >= 0")
    return q


# ---------------------------------------------------------------------- evaluación fila por fila
def _analyzer_for(db: Database, q: Query) -> Analyzer:
    idx = q.index_for(q.match_col, IndexKind.INVERTED)
    if idx is not None:
        assert isinstance(idx.structure, InvertedIndex)
        return idx.structure.analyzer
    return Analyzer(str(db.settings.get("text.analyzer", "spanish")))


def _row_checks(
    db: Database, q: Query, *, scalar: Sequence[BoundPredicate] | None, spatial: bool, match: bool
) -> Callable[[tuple[Any, ...]], bool]:
    """Filtro sobre los valores de una fila con los predicados que el acceso no resolvió."""
    checks: list[Callable[[tuple[Any, ...]], bool]] = []
    if scalar:
        preds = list(scalar)
        checks.append(lambda values: matches_all(preds, values))
    if spatial and q.spatial is not None:
        col = q.spatial_col
        sp = q.spatial
        if isinstance(sp, ast.WithinRadius):
            (cx, cy), r = sp.center, sp.radius
            checks.append(lambda values: haversine_km(cx, cy, values[col][0], values[col][1]) <= r)
        else:
            (x1, y1), (x2, y2) = sp.low, sp.high
            checks.append(lambda values: x1 <= values[col][0] <= x2 and y1 <= values[col][1] <= y2)
    if match and q.match is not None:
        col_m = q.match_col
        try:
            matcher = compile_match(q.match.query, _analyzer_for(db, q))
        except InvertedIndexError as exc:
            raise SemanticError(str(exc)) from None
        checks.append(lambda values: matcher(values[col_m]))
    if not checks:
        return lambda values: True
    if len(checks) == 1:
        return checks[0]
    return lambda values: all(check(values) for check in checks)


def _compute(q: Query, values: tuple[Any, ...], known: dict[Any, float]) -> dict[Any, float]:
    """Completa las expresiones calculadas que el índice no entregó."""
    for key in q.needs:
        if key in known:
            continue
        if key[0] == "gdist":
            _, col, (px, py) = key
            known[key] = haversine_km(px, py, values[col][0], values[col][1])
        elif key[0] == "vdist":
            _, col, metric, qvec = key
            vec = np.asarray(values[col], dtype=np.float32).reshape(1, -1)
            known[key] = float(distances(metric, np.asarray(qvec, dtype=np.float32), vec)[0])
        elif key[0] == "score":
            raise SemanticError("COSINE_SCORE() necesita un índice INVERTED sobre la columna del MATCH")
    return known


def _sort_value(q: Query, row: Row) -> Any:
    key = q.order_key
    if key[0] == "column":
        return row[1][key[1]]
    return row[2][key]


def _top(q: Query, rows: Iterator[Row]) -> list[Row]:
    """Aplica ``ORDER BY`` / ``LIMIT``: Top-K con un heap si hay límite."""
    if q.order_key is None:
        return list(rows if q.limit is None else itertools.islice(rows, q.limit))
    counter = itertools.count()
    keyed = ((_sort_value(q, row), next(counter), row) for row in rows)
    if q.limit is None:
        # sorted es estable también con reverse: a igual clave se conserva el orden de llegada
        return [t[2] for t in sorted(keyed, key=lambda t: t[0], reverse=q.descending)]
    if q.descending:
        best = heapq.nlargest(q.limit, keyed, key=lambda t: (t[0], -t[1]))
    else:
        best = heapq.nsmallest(q.limit, keyed, key=lambda t: (t[0], t[1]))
    return [t[2] for t in best]


# ---------------------------------------------------------------------- estimaciones
def _selectivity(q: Query) -> float:
    """Fracción estimada de filas que pasan todos los filtros del WHERE."""
    table = q.table
    n = max(1, table.file.record_count)
    sel = 1.0
    ranged: set[int] = set()
    bounds = range_bounds(q.scalar)
    for p in q.scalar:
        if p.op == "=":
            unique = table.schema.pk_index == p.col_index
            sel *= 1.0 / n if unique else DEFAULT_EQ_SELECTIVITY
        elif p.op == "!=":
            sel *= 1.0 - DEFAULT_EQ_SELECTIVITY
        elif p.col_index not in ranged:
            ranged.add(p.col_index)
            b = bounds[p.col_index]
            tree = next((i.structure for i in table.indexes_on(p.col_index) if isinstance(i.structure, BPlusTree)), None)
            if tree is not None:
                sel *= _range_selectivity(b, tree.min_key, tree.max_key, p.column.type != ColumnType.CHAR)
            else:
                sel *= DEFAULT_RANGE_SELECTIVITY
    if q.spatial is not None:
        sel *= DEFAULT_SPATIAL_SELECTIVITY
    if q.match is not None:
        sel *= DEFAULT_TEXT_SELECTIVITY
    return max(sel, 1.0 / n)


@dataclass
class Candidate:
    """Una forma de ejecutar la consulta, con su costo estimado."""

    type: str
    structure: str
    strategy: str | None
    estimated_io: int
    detail: str
    run: Callable[[], Iterator[Row]]
    index: str | None = None
    column: str | None = None
    predicate: str | None = None
    residual: str | None = None
    estimated_rows: float = 0.0
    ordered: bool = False  # las filas ya salen en el orden del ORDER BY
    priority: int = 9  # orden de las reglas (menor = primero)

    def summary(self, chosen: bool) -> dict[str, Any]:
        return {
            "type": self.type, "structure": self.structure, "index": self.index, "column": self.column,
            "estimated_io": self.estimated_io, "chosen": chosen, "strategy": self.strategy,
        }


# ---------------------------------------------------------------------- planificador
class MultimodalPlanner:
    def __init__(self, db: Database, q: Query, mode: str) -> None:
        self.db = db
        self.q = q
        self.mode = mode
        self.table = q.table
        self.n = max(1, q.table.file.record_count)
        self.sel = _selectivity(q)
        self.extra: dict[str, Any] = {}  # detalles reales de la ejecución (texto, vector, espacial)
        self.scalar_plan: Plan = make_plan(self.table, q.scalar, mode)

    # -- textos descriptivos
    def _describe(self, preds: Sequence[Any]) -> str | None:
        return " AND ".join(str(p) for p in preds if p is not None) or None

    def _others(self, *, scalar: bool = True, spatial: bool = True, match: bool = True) -> str | None:
        parts: list[Any] = []
        if scalar:
            parts.extend(self.q.scalar)
        if spatial:
            parts.append(self.q.spatial)
        if match:
            parts.append(self.q.match)
        return self._describe(parts)

    def _fetch_cost(self, rows: float) -> int:
        return math.ceil(rows)

    # -- acceso por filtros (pre-filtering o consulta sin ranking por índice)
    def _scalar_rows(self) -> Iterator[Row]:
        from backend.sql.executor import _rows_for_plan

        check = _row_checks(self.db, self.q, scalar=None, spatial=True, match=True)
        for rid, values in _rows_for_plan(self.table, self.scalar_plan):
            if check(values):
                yield rid, values, {}

    def _scores(self, index: Index, allowed: set[RID] | None = None, k: int | None = None) -> list[tuple[RID, float]]:
        """Ejecuta el MATCH en el índice invertido y registra sus estadísticas."""
        q = self.q
        index = self.table.fresh_index(index)
        s = index.structure
        assert isinstance(s, InvertedIndex) and q.match is not None
        try:
            result = s.search(q.match.query, k, ranked=q.needs_score or q.order_key == ("score",), allowed=allowed)
        except InvertedIndexError as exc:
            raise SemanticError(str(exc)) from None
        self.extra["text"] = {
            "mode": result.mode,
            "scoring": result.scoring,
            "matched": result.total,
            "terms": [t.as_dict() for t in result.terms],
            "rebuilt": index.rebuilt,
        }
        return result.hits

    def _text_rows(self, index: Index) -> Iterator[Row]:
        check = _row_checks(self.db, self.q, scalar=self.q.scalar, spatial=True, match=False)
        for rid, score in self._scores(index):
            values = self.table.fetch(rid)
            if check(values):
                yield rid, values, {("score",): score}

    def _spatial_rows(self, index: Index) -> Iterator[Row]:
        q = self.q
        tree = index.structure
        assert isinstance(tree, RTree) and q.spatial is not None
        trace = SearchTrace()
        sp = q.spatial
        check = _row_checks(self.db, q, scalar=q.scalar, spatial=False, match=True)
        col = q.spatial_col
        if isinstance(sp, ast.WithinRadius):
            (cx, cy), r = sp.center, sp.radius
            # cota inferior de la distancia: grados ponderados con la latitud más desfavorable del círculo
            far_lat = min(89.0, abs(cy) + r / KM_PER_DEG_LAT)
            hits: Iterator[tuple[RID, Any, float]] = tree.search_radius((cx, cy), r, geo_weights(far_lat), trace)
            self.extra["spatial"] = {"kind": "radius", "center": [cx, cy], "radius_km": r}
            exact = lambda values: haversine_km(cx, cy, values[col][0], values[col][1]) <= r  # noqa: E731
        else:
            hits = ((rid, lo, 0.0) for rid, lo in tree.search_window(sp.low, sp.high, trace))
            self.extra["spatial"] = {"kind": "box", "box": [*sp.low, *sp.high]}
            exact = lambda values: True  # noqa: E731
        candidates = 0
        try:
            for rid, _, _ in hits:
                candidates += 1
                values = self.table.fetch(rid)
                if exact(values) and check(values):  # refinamiento con la distancia exacta
                    yield rid, values, {}
        finally:
            self.extra["spatial"].update(
                {"candidates": candidates, "nodes_visited": trace.visited, "leaves_visited": trace.leaves,
                 "mbrs": trace.nodes}
            )

    def _attach_scores(self, rows: Iterator[Row], index: Index) -> Iterator[Row]:
        """El acceso no fue por el índice invertido pero la consulta pide COSINE_SCORE()."""
        scores = dict(self._scores(index))
        for rid, values, known in rows:
            if rid in scores:
                known[("score",)] = scores[rid]
                yield rid, values, known

    def _filter_candidates(self) -> list[Candidate]:
        """Caminos que resuelven primero los filtros del WHERE."""
        q = self.q
        out: list[Candidate] = []
        text_index = q.index_for(q.match_col, IndexKind.INVERTED)
        geo_index = q.index_for(q.spatial_col, IndexKind.RTREE)
        if q.match is not None and text_index is not None:
            s = text_index.structure
            assert isinstance(s, InvertedIndex)
            rows = self.n * DEFAULT_TEXT_SELECTIVITY
            vocab = max(1, math.ceil(math.log2(max(2, s.vocab.num_pages()))))
            cost = 3 * (vocab + max(1, math.ceil(rows / s.postings_per_page))) + self._fetch_cost(rows)
            out.append(Candidate(
                "InvertedIndexScan", "INVERTED", None, cost,
                f"Índice invertido: búsqueda binaria en vocab.dic (~{vocab} lecturas por término), lectura de las "
                f"listas de posteo y mezcla con dos punteros; luego 1 lectura por documento",
                lambda: self._text_rows(text_index), index=text_index.name, column=q.match.column,
                predicate=str(q.match), residual=self._others(match=False), estimated_rows=rows, priority=1,
            ))
        if q.spatial is not None and geo_index is not None:
            tree = geo_index.structure
            assert isinstance(tree, RTree)
            rows = self.n * DEFAULT_SPATIAL_SELECTIVITY
            cost = tree.height + math.ceil(rows / tree.leaf_capacity) + self._fetch_cost(rows)
            radius = isinstance(q.spatial, ast.WithinRadius)
            out.append(Candidate(
                "RTreeRadiusScan" if radius else "RTreeWindowScan", "RTREE", None, cost,
                f"R-Tree de altura {tree.height}: se podan las ramas cuyo MBR no interseca la región "
                f"({'MINDIST > r' if radius else 'MBR ∩ W = ∅'}); refinamiento con la distancia exacta",
                lambda: self._spatial_rows(geo_index), index=geo_index.name, column=q.spatial.column,
                predicate=str(q.spatial), residual=self._others(spatial=False), estimated_rows=rows, priority=2,
            ))
        a = self.scalar_plan.access
        residual = [str(p) for p in self.scalar_plan.residual]
        if q.spatial is not None:
            residual.append(str(q.spatial))
        if q.match is not None:
            residual.append(str(q.match))
        out.append(Candidate(
            a.type, a.structure, None, a.estimated_io, a.detail, self._scalar_rows, index=a.index_name,
            column=a.column, predicate=a.predicate, residual=" AND ".join(residual) or None,
            estimated_rows=a.estimated_rows, priority=3 if a.type != "SeqScan" else 8,
        ))
        if q.needs_score and text_index is not None:
            for c in out:
                if c.type != "InvertedIndexScan":
                    inner = c.run
                    c.run = lambda inner=inner: self._attach_scores(inner(), text_index)  # type: ignore[misc]
                    c.detail += "; la relevancia se obtiene del índice invertido"
        return out

    # -- acceso por ranking (post-filtering)
    def _ivf_rows(self, index: Index, key: Any) -> Iterator[Row]:
        q = self.q
        index = self.table.fresh_index(index)
        s = index.structure
        assert isinstance(s, IVFIndex)
        probes = int(self.db.settings["ivf.probes"])
        k = q.limit if not q.has_filters else None
        info = s.search(key[3], k, probes)
        hits = info.pop("hits")
        check = _row_checks(self.db, q, scalar=q.scalar, spatial=True, match=True)
        fetched = 0
        try:
            for rid, dist in hits:
                values = self.table.fetch(rid)
                fetched += 1
                if check(values):
                    yield rid, values, {key: dist}
        finally:
            info.update({"metric": s.metric, "fetched": fetched, "rebuilt": index.rebuilt})
            self.extra["vector"] = info

    def _knn_rows(self, index: Index, key: Any) -> Iterator[Row]:
        q = self.q
        tree = index.structure
        assert isinstance(tree, RTree)
        px, py = key[2]
        trace = SearchTrace()
        check = _row_checks(self.db, q, scalar=q.scalar, spatial=True, match=True)
        self.extra["spatial"] = {"kind": "knn", "center": [px, py], "k": q.limit}
        candidates = 0
        try:
            for rid, _, _ in tree.nearest((px, py), geo_weights(py), trace):
                values = self.table.fetch(rid)
                candidates += 1
                if check(values):
                    yield rid, values, {key: haversine_km(px, py, values[key[1]][0], values[key[1]][1])}
        finally:
            self.extra["spatial"].update(
                {"candidates": candidates, "nodes_visited": trace.visited, "leaves_visited": trace.leaves,
                 "mbrs": trace.nodes}
            )

    def _ranked_text_rows(self, index: Index) -> Iterator[Row]:
        q = self.q
        others = bool(q.scalar) or q.spatial is not None
        check = _row_checks(self.db, q, scalar=q.scalar, spatial=True, match=False)
        for rid, score in self._scores(index, k=None if others else q.limit):
            values = self.table.fetch(rid)
            if check(values):
                yield rid, values, {("score",): score}

    def _prefiltered_text_rows(self, index: Index) -> Iterator[Row]:
        """Pre-filtering textual: los RID del índice relacional restringen el ranking."""
        q = self.q
        access = self.scalar_plan.access
        assert access.index is not None
        if access.type == "IndexScan":
            allowed = set(access.index.search(access.eq_value))
        else:
            b = access.bounds
            assert b is not None
            allowed = set(access.index.range(b.low, b.high, b.low_inclusive, b.high_inclusive))
        self.extra["prefilter_rids"] = len(allowed)
        check = _row_checks(self.db, q, scalar=self.scalar_plan.residual, spatial=True, match=False)
        others = bool(self.scalar_plan.residual) or q.spatial is not None
        for rid, score in self._scores(index, allowed=allowed, k=None if others else q.limit):
            values = self.table.fetch(rid)
            if check(values):
                yield rid, values, {("score",): score}

    def _ranking_candidates(self) -> list[Candidate]:
        q = self.q
        key = q.order_key
        if key is None or key[0] == "column":
            return []
        k = q.limit if q.limit is not None else self.n
        needed = min(self.n, k / self.sel)  # candidatos que hay que revisar para juntar k
        out: list[Candidate] = []
        if key[0] == "vdist" and not q.descending:
            index = q.index_for(key[1], IndexKind.IVF)
            if index is not None and isinstance(index.structure, IVFIndex) and index.structure.metric == key[2]:
                s = index.structure
                probes = min(int(self.db.settings["ivf.probes"]), max(1, s.num_lists))
                per_list = max(1.0, (s.lists.num_pages() - 1) / max(1, s.num_lists))
                scanned = probes * s.entry_count / max(1, s.num_lists)
                fetches = min(scanned, needed)
                cost = s.dir_pages + math.ceil(probes * per_list) + self._fetch_cost(fetches)
                out.append(Candidate(
                    "IVFScan", "IVF", "post-filter" if q.has_filters else None, cost,
                    f"IVF: {s.dir_pages} página(s) de centroides + las cadenas de {probes} de {s.num_lists} celdas "
                    f"(~{math.ceil(per_list)} páginas cada una); los candidatos se traen por RID en orden de distancia"
                    + (" y se les aplica el filtro hasta completar el límite" if q.has_filters else ""),
                    lambda: self._ivf_rows(index, key), index=index.name, column=index.info.column,
                    predicate=f"ORDER BY {index.info.column} {'<=>' if s.metric == 'COSINE' else '<->'} q",
                    residual=self._others(), estimated_rows=fetches, ordered=True, priority=0,
                ))
        elif key[0] == "gdist" and not q.descending and q.limit is not None:
            index = q.index_for(key[1], IndexKind.RTREE)
            if index is not None:
                tree = index.structure
                assert isinstance(tree, RTree)
                cost = tree.height + math.ceil(needed / tree.leaf_capacity) + self._fetch_cost(needed)
                out.append(Candidate(
                    "RTreeKNN", "RTREE", "post-filter" if q.has_filters else None, cost,
                    f"k-NN sobre el R-Tree (altura {tree.height}): cola de prioridad ordenada por MINDIST; "
                    "los vecinos salen en orden exacto de distancia"
                    + (" y se filtran hasta completar k" if q.has_filters else ""),
                    lambda: self._knn_rows(index, key), index=index.name, column=index.info.column,
                    predicate=f"ORDER BY {index.info.column} <-> ({key[2][0]}, {key[2][1]}) LIMIT {q.limit}",
                    residual=self._others(), estimated_rows=needed, ordered=True, priority=0,
                ))
        elif key[0] == "score" and q.descending:
            index = q.index_for(q.match_col, IndexKind.INVERTED)
            if index is not None:
                s2 = index.structure
                assert isinstance(s2, InvertedIndex)
                vocab = max(1, math.ceil(math.log2(max(2, s2.vocab.num_pages()))))
                matched = self.n * DEFAULT_TEXT_SELECTIVITY
                text_cost = 3 * (vocab + max(1, math.ceil(matched / s2.postings_per_page))) + \
                    math.ceil(matched / s2.docs_per_page)
                others = bool(q.scalar) or q.spatial is not None
                sel_others = self.sel / DEFAULT_TEXT_SELECTIVITY
                fetches = min(matched, k / max(sel_others, 1e-9)) if others else min(matched, k)
                out.append(Candidate(
                    "InvertedIndexScan", "INVERTED", "post-filter" if others else None,
                    text_cost + self._fetch_cost(fetches),
                    f"Índice invertido con {s2.scoring}: se acumulan los productos sobre las listas de los términos "
                    "de la consulta, se normaliza y se toma el Top-K con un heap"
                    + ("; los documentos se traen en orden de relevancia y se filtran" if others else ""),
                    lambda: self._ranked_text_rows(index), index=index.name, column=q.match.column,  # type: ignore[union-attr]
                    predicate=str(q.match), residual=self._others(match=False), estimated_rows=fetches,
                    ordered=True, priority=0,
                ))
                access = self.scalar_plan.access
                if access.type in ("IndexScan", "IndexRangeScan") and access.index is not None:
                    index_only = max(1, access.estimated_io - math.ceil(access.estimated_rows))
                    out.append(Candidate(
                        "InvertedIndexScan", "INVERTED", "pre-filter",
                        index_only + text_cost + self._fetch_cost(min(k, access.estimated_rows)),
                        f"Pre-filtering: {access.type} sobre {access.index.name} entrega los RID que cumplen "
                        f"{access.predicate} (sin traer los registros); el ranking del índice invertido solo "
                        "considera esos documentos",
                        lambda: self._prefiltered_text_rows(index), index=f"{access.index.name} + {index.name}",
                        column=q.match.column, predicate=f"{access.predicate} AND {q.match}",  # type: ignore[union-attr]
                        residual=self._describe([*self.scalar_plan.residual, q.spatial]),
                        estimated_rows=min(k, access.estimated_rows), ordered=True, priority=0,
                    ))
        return out

    # -- elección
    def candidates(self) -> tuple[Candidate, list[Candidate]]:
        q = self.q
        ranking = self._ranking_candidates()
        filters = self._filter_candidates()
        ranked_by_index = bool(ranking)
        if ranked_by_index and q.has_filters:
            for c in filters:
                c.strategy = c.strategy or "pre-filter"
                if q.order_key is not None and q.order_key[0] != "column":
                    c.detail += "; el ranking se calcula de forma exacta sobre las filas filtradas (Top-K con heap)"
        elif q.order_key is not None and q.order_key[0] in ("vdist", "gdist") and not ranked_by_index:
            for c in filters:
                c.detail += "; k-NN exacto: se calcula la distancia de cada fila y se mantiene el Top-K en un heap"
        # una fila calculada que solo puede dar el índice invertido descarta los caminos que no lo usan
        pool = ranking + filters
        if q.needs_score and q.index_for(q.match_col, IndexKind.INVERTED) is None:
            raise SemanticError(
                f"COSINE_SCORE() necesita un índice INVERTED sobre '{q.match.column}' "  # type: ignore[union-attr]
                f"(CREATE INDEX ... ON {self.table.name}({q.match.column}) USING INVERTED)"  # type: ignore[union-attr]
            )
        forced = str(self.db.settings.get("hybrid.strategy", "auto"))
        choice = pool
        if ranked_by_index and q.has_filters and forced in ("pre", "post"):
            wanted = "pre-filter" if forced == "pre" else "post-filter"
            subset = [c for c in pool if c.strategy == wanted]
            choice = subset or pool
        elif ranked_by_index and not q.has_filters:
            choice = ranking  # ranking puro: el índice de ranking siempre aplica
        elif not ranked_by_index and self.mode == "rules":
            first = min(c.priority for c in pool)
            choice = [c for c in pool if c.priority == first]
        chosen = min(choice, key=lambda c: (c.estimated_io, c.priority))
        return chosen, pool


# ---------------------------------------------------------------------- ejecución
def _jsonable(value: Any) -> Any:
    if isinstance(value, tuple):
        return [round(v, 6) if isinstance(v, float) else v for v in value]
    return value


def plan_select(db: Database, table: Table, stmt: ast.Select, mode: str) -> tuple[MultimodalPlanner, Candidate, dict[str, Any]]:
    q = resolve(table, stmt)
    planner = MultimodalPlanner(db, q, mode)
    chosen, pool = planner.candidates()
    info = {
        "type": chosen.type,
        "table": table.name,
        "structure": chosen.structure,
        "index": chosen.index,
        "column": chosen.column,
        "predicate": chosen.predicate,
        "filter": chosen.residual,
        "estimated_io": chosen.estimated_io,
        "estimated_rows": round(chosen.estimated_rows, 1),
        "detail": chosen.detail,
        "planner": mode,
        "strategy": chosen.strategy,
        "order_by": None if q.order_key is None else str(stmt.order_by),
        "limit": q.limit,
        "candidates": [c.summary(c is chosen) for c in pool],
    }
    return planner, chosen, info


def run_select(
    db: Database, table: Table, stmt: ast.Select, page: int, page_size: int, mode: str
) -> tuple[list[str], list[list[Any]], int, dict[str, Any]]:
    """Ejecuta un ``SELECT`` multimodal; devuelve columnas, filas de la página, total y plan."""
    planner, chosen, info = plan_select(db, table, stmt, mode)
    q = planner.q
    source = chosen.run()
    rows = ((rid, values, _compute(q, values, known)) for rid, values, known in source)
    try:
        if chosen.ordered and q.order_key is not None:
            # el índice ya entrega en orden: basta cortar en el límite
            result = list(rows if q.limit is None else itertools.islice(rows, q.limit))
        else:
            result = _top(q, rows)
    finally:
        source.close()  # deja de leer y registra las estadísticas de la búsqueda
    total = len(result)
    start = (page - 1) * page_size
    out_rows = []
    for _, values, known in result[start : start + page_size]:
        out_rows.append([
            _jsonable(values[o.column]) if o.column is not None else round(known[o.key], 6) + 0.0  # evita -0.0
            for o in q.outputs
        ])
    info.update(planner.extra)
    if chosen.strategy == "post-filter" and q.limit is not None and total < q.limit and chosen.type == "IVFScan":
        info["warning"] = (
            f"el post-filtering devolvió {total} de {q.limit} filas: el filtro descartó a los demás candidatos "
            "de las celdas sondeadas (aumente ivf.probes o use SET hybrid.strategy = pre)"
        )
    return [o.name for o in q.outputs], out_rows, total, info


def matching_rids(db: Database, table: Table, where: list[ast.Predicate], mode: str) -> tuple[list[Any], dict[str, Any]]:
    """RID de las filas que cumplen un WHERE con predicados multimodales (para DELETE)."""
    stmt = ast.Select(table.name, None, where)
    planner, chosen, info = plan_select(db, table, stmt, mode)
    rids = [rid for rid, _, _ in chosen.run()]
    info.update(planner.extra)
    return rids, info
