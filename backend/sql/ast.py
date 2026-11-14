"""Árbol de sintaxis abstracta de las sentencias SQL soportadas."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Union

from backend.storage.catalog import IndexKind, Organization
from backend.storage.record import ColumnType


@dataclass(frozen=True)
class Position:
    line: int
    column: int
    offset: int

    def as_dict(self) -> dict[str, int]:
        return {"line": self.line, "column": self.column, "offset": self.offset}


# ---------------------------------------------------------------------- predicados
@dataclass(frozen=True)
class Comparison:
    """``columna op literal`` con op en ``= != < <= > >=``."""

    column: str
    op: str
    value: Any

    def __str__(self) -> str:
        return f"{self.column} {self.op} {format_literal(self.value)}"


@dataclass(frozen=True)
class Between:
    """``columna BETWEEN low AND high`` (ambas cotas inclusivas)."""

    column: str
    low: Any
    high: Any

    def __str__(self) -> str:
        return f"{self.column} BETWEEN {format_literal(self.low)} AND {format_literal(self.high)}"


Point = tuple[float, float]


@dataclass(frozen=True)
class WithinRadius:
    """``columna WITHIN_RADIUS((lon, lat), r)``: puntos a lo más a ``r`` km del centro."""

    column: str
    center: Point
    radius: float

    def __str__(self) -> str:
        return f"{self.column} WITHIN_RADIUS(({self.center[0]}, {self.center[1]}), {self.radius})"


@dataclass(frozen=True)
class WithinBox:
    """``columna WITHIN_BOX((x1, y1), (x2, y2))``: puntos dentro del rectángulo."""

    column: str
    low: Point
    high: Point

    def __str__(self) -> str:
        return (f"{self.column} WITHIN_BOX(({self.low[0]}, {self.low[1]}), "
                f"({self.high[0]}, {self.high[1]}))")


@dataclass(frozen=True)
class Match:
    """``columna MATCH 'consulta'``: booleana (AND / OR / NOT) o de texto libre."""

    column: str
    query: str

    def __str__(self) -> str:
        return f"{self.column} MATCH {format_literal(self.query)}"


Predicate = Union[Comparison, Between, WithinRadius, WithinBox, Match]
SCALAR_PREDICATES = (Comparison, Between)


@dataclass(frozen=True)
class Expr:
    """Expresión de la lista de selección o del ``ORDER BY``.

    * ``column``: una columna (o, en ``ORDER BY``, el alias de otra expresión);
    * ``score``: ``COSINE_SCORE()``, la relevancia del ``MATCH`` de la consulta;
    * ``distance``: ``columna <-> operando`` (L2 o km) o ``columna <=> operando`` (coseno).
    """

    kind: str
    column: str | None = None
    op: str | None = None
    operand: Any = None

    def __str__(self) -> str:
        if self.kind == "score":
            return "COSINE_SCORE()"
        if self.kind == "distance":
            return f"{self.column} {self.op} {format_operand(self.operand)}"
        return str(self.column)


@dataclass(frozen=True)
class SelectItem:
    expr: Expr
    alias: str | None = None

    @property
    def name(self) -> str:
        return self.alias or str(self.expr)


@dataclass(frozen=True)
class OrderBy:
    expr: Expr
    descending: bool = False

    def __str__(self) -> str:
        return f"{self.expr} {'DESC' if self.descending else 'ASC'}"


def format_operand(value: Any) -> str:
    if isinstance(value, tuple):
        if len(value) > 6:
            head = ", ".join(f"{v:g}" for v in value[:3])
            return f"'[{head}, … ({len(value)} dims)]'"
        inner = ", ".join(f"{v:g}" for v in value)
        return f"({inner})" if len(value) == 2 else f"'[{inner}]'"
    if isinstance(value, str) and len(value) > 48:
        return format_literal(value[:45] + "…")
    return format_literal(value)


def format_literal(value: Any) -> str:
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    return repr(value) if isinstance(value, float) else str(value)


# ---------------------------------------------------------------------- sentencias
@dataclass
class Statement:
    sql: str = field(default="", kw_only=True)
    position: Position | None = field(default=None, kw_only=True)

    @property
    def kind(self) -> str:
        raise NotImplementedError


@dataclass
class ColumnDef:
    name: str
    type: ColumnType
    size: int | None = None
    primary_key: bool = False


@dataclass
class CreateTable(Statement):
    name: str
    columns: list[ColumnDef]
    organization: Organization = Organization.HEAP

    @property
    def kind(self) -> str:
        return "CREATE TABLE"


@dataclass
class CreateIndex(Statement):
    name: str
    table: str
    column: str
    method: IndexKind = IndexKind.BTREE
    options: dict[str, str] = field(default_factory=dict)  # WITH (clave = valor, ...)

    @property
    def kind(self) -> str:
        return "CREATE INDEX"


@dataclass
class Insert(Statement):
    table: str
    rows: list[list[Any]]

    @property
    def kind(self) -> str:
        return "INSERT"


@dataclass
class Select(Statement):
    table: str
    columns: list[str] | None  # None = '*' (o una lista con expresiones: ver ``items``)
    where: list[Predicate] = field(default_factory=list)
    # Lista de selección con expresiones (COSINE_SCORE(), distancias, alias); None si
    # son solo columnas o '*'.
    items: list[SelectItem] | None = None
    order_by: OrderBy | None = None
    limit: int | None = None

    @property
    def kind(self) -> str:
        return "SELECT"

    @property
    def is_relational(self) -> bool:
        """Consulta de la Entrega 1: solo columnas y predicados de comparación."""
        return (
            self.items is None
            and self.order_by is None
            and self.limit is None
            and all(isinstance(p, SCALAR_PREDICATES) for p in self.where)
        )


@dataclass
class Delete(Statement):
    table: str
    where: list[Predicate] = field(default_factory=list)

    @property
    def kind(self) -> str:
        return "DELETE"


@dataclass
class DropTable(Statement):
    name: str

    @property
    def kind(self) -> str:
        return "DROP TABLE"


@dataclass
class DropIndex(Statement):
    name: str

    @property
    def kind(self) -> str:
        return "DROP INDEX"


@dataclass
class Copy(Statement):
    """``COPY tabla FROM 'archivo.csv'`` (carga masiva desde MINIDB_DATA_DIR)."""

    table: str
    path: str

    @property
    def kind(self) -> str:
        return "COPY"


@dataclass
class Set(Statement):
    """``SET ivf.probes = 5``: parámetro de sesión del motor."""

    name: str
    value: Any

    @property
    def kind(self) -> str:
        return "SET"


@dataclass
class Explain(Statement):
    """``EXPLAIN SELECT ...``: devuelve el plan sin ejecutar la consulta."""

    statement: Select | Delete

    @property
    def kind(self) -> str:
        return "EXPLAIN"
