"""Tipos compartidos por las organizaciones de archivo (Heap y Sequential)."""

from __future__ import annotations

from typing import Callable, Union

# Filtro evaluado sobre los bytes crudos del registro: recibe (buffer de la página,
# offset del registro) y decide si el registro califica. Así el scan solo
# decodifica los registros que pasan el filtro.
RawFilter = Callable[[Union[bytes, bytearray, memoryview], int], bool]


class DuplicateKeyError(Exception):
    """Violación de unicidad de la clave primaria."""


class RecordNotFoundError(Exception):
    """El RID no apunta a un registro activo."""
