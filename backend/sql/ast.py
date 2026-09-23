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


Predicate = Union[Comparison, Between]


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
    columns: list[str] | None  # None = '*'
    where: list[Predicate] = field(default_factory=list)

    @property
    def kind(self) -> str:
        return "SELECT"


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
class Explain(Statement):
    """``EXPLAIN SELECT ...``: devuelve el plan sin ejecutar la consulta."""

    statement: Select | Delete

    @property
    def kind(self) -> str:
        return "EXPLAIN"
