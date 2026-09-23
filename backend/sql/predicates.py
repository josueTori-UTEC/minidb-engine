"""Predicados ligados al esquema: validación de tipos, evaluación y filtros crudos."""

from __future__ import annotations

import operator
import struct
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from backend.files.common import RawFilter
from backend.sql import ast
from backend.sql.errors import SemanticError
from backend.storage.record import Column, ColumnType, Schema, SchemaError, encode_str

_OPS: dict[str, Callable[[Any, Any], bool]] = {
    "=": operator.eq,
    "!=": operator.ne,
    "<": operator.lt,
    "<=": operator.le,
    ">": operator.gt,
    ">=": operator.ge,
}


@dataclass(frozen=True)
class BoundPredicate:
    """Predicado con la columna resuelta y el literal llevado al dominio de la columna."""

    source: ast.Predicate
    col_index: int
    column: Column
    op: str  # '=', '!=', '<', '<=', '>', '>=' o 'between'
    value: Any
    high: Any = None

    def __str__(self) -> str:
        return str(self.source)

    def matches(self, values: Sequence[Any]) -> bool:
        v = values[self.col_index]
        if self.op == "between":
            return self.value <= v <= self.high
        return _OPS[self.op](v, self.value)


def bind(schema: Schema, predicates: Sequence[ast.Predicate]) -> list[BoundPredicate]:
    bound = []
    for pred in predicates:
        if not schema.has_column(pred.column):
            raise SemanticError(f"no existe la columna '{pred.column}'")
        idx = schema.index_of(pred.column)
        col = schema.columns[idx]
        try:
            if isinstance(pred, ast.Between):
                bound.append(BoundPredicate(pred, idx, col, "between", col.comparable(pred.low), col.comparable(pred.high)))
            else:
                bound.append(BoundPredicate(pred, idx, col, pred.op, col.comparable(pred.value)))
        except SchemaError as exc:
            raise SemanticError(str(exc)) from None
    return bound


def matches_all(predicates: Sequence[BoundPredicate], values: Sequence[Any]) -> bool:
    return all(p.matches(values) for p in predicates)


def compile_raw_filter(schema: Schema, predicates: Sequence[BoundPredicate]) -> RawFilter | None:
    """Filtro sobre los bytes del registro: extrae solo las columnas del WHERE.

    INT/FLOAT se desempaquetan con ``struct``; CHAR se compara como bytes UTF-8
    rellenados con ``\\x00`` (mismo orden que los strings), sin decodificar.
    """
    if not predicates:
        return None
    checks: list[Callable[[Any, int], bool]] = []
    for p in predicates:
        offset = schema.offsets[p.col_index]
        field = struct.Struct("<" + p.column.fmt)
        unpack = field.unpack_from
        if p.column.type == ColumnType.CHAR:
            size = p.column.size or 0

            def conv(v: Any, size: int = size) -> bytes:
                return encode_str(v, size).ljust(size, b"\x00")

            lo = conv(p.value)
            hi = conv(p.high) if p.op == "between" else None
        else:
            lo, hi = p.value, p.high
        if p.op == "between":
            checks.append(lambda buf, off, u=unpack, o=offset, a=lo, b=hi: a <= u(buf, off + o)[0] <= b)
        else:
            fn = _OPS[p.op]
            checks.append(lambda buf, off, u=unpack, o=offset, f=fn, a=lo: f(u(buf, off + o)[0], a))
    if len(checks) == 1:
        return checks[0]
    return lambda buf, off: all(check(buf, off) for check in checks)


@dataclass
class ColumnBounds:
    """Cotas combinadas de los predicados de rango sobre una columna."""

    low: Any = None
    low_inclusive: bool = True
    high: Any = None
    high_inclusive: bool = True
    predicates: list[BoundPredicate] | None = None

    def add_low(self, value: Any, inclusive: bool) -> None:
        if self.low is None or value > self.low or (value == self.low and not inclusive):
            self.low, self.low_inclusive = value, inclusive

    def add_high(self, value: Any, inclusive: bool) -> None:
        if self.high is None or value < self.high or (value == self.high and not inclusive):
            self.high, self.high_inclusive = value, inclusive

    def describe(self, column: str) -> str:
        parts = []
        if self.low is not None:
            parts.append(f"{column} {'>=' if self.low_inclusive else '>'} {ast.format_literal(self.low)}")
        if self.high is not None:
            parts.append(f"{column} {'<=' if self.high_inclusive else '<'} {ast.format_literal(self.high)}")
        return " AND ".join(parts)


def range_bounds(predicates: Sequence[BoundPredicate]) -> dict[int, ColumnBounds]:
    """Agrupa por columna los predicados <, <=, >, >= y BETWEEN."""
    out: dict[int, ColumnBounds] = {}
    for p in predicates:
        if p.op not in ("<", "<=", ">", ">=", "between"):
            continue
        b = out.setdefault(p.col_index, ColumnBounds(predicates=[]))
        assert b.predicates is not None
        b.predicates.append(p)
        if p.op == "between":
            b.add_low(p.value, True)
            b.add_high(p.high, True)
        elif p.op in (">", ">="):
            b.add_low(p.value, p.op == ">=")
        else:
            b.add_high(p.value, p.op == "<=")
    return out
