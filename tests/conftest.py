"""Fixtures compartidas por los tests del motor."""

import pytest

from backend.storage.disk_manager import DiskCounter
from backend.storage.record import Column, ColumnType, Schema


@pytest.fixture
def counter() -> DiskCounter:
    return DiskCounter()


@pytest.fixture
def empleados_schema() -> Schema:
    """Esquema del enunciado: registro de 62 bytes."""
    return Schema(
        [
            Column("id", ColumnType.INT, primary_key=True),
            Column("nombre", ColumnType.CHAR, 30),
            Column("dept", ColumnType.CHAR, 20),
            Column("salario", ColumnType.FLOAT),
        ]
    )


def make_row(i: int) -> tuple:
    """Fila determinista para tests: (id, nombre, dept, salario)."""
    return (i, f"Empleado {i}", f"Dept {i % 7}", 1000.0 + i)
