"""Esquemas de tabla y serialización de registros de longitud fija con ``struct``.

Tipos soportados y su formato:

* ``INT``   -> ``'i'`` (4 bytes, entero con signo de 32 bits)
* ``FLOAT`` -> ``'d'`` (8 bytes, doble precisión)
* ``CHAR(n)`` / ``VARCHAR(n)`` / ``TEXT(n)`` -> ``'{n}s'`` (UTF-8 rellenado con ``\\x00``;
  se trunca por bytes sin cortar un carácter multibyte)
* ``POINT`` -> ``'dd'`` (16 bytes: ``x`` = longitud, ``y`` = latitud)
* ``VECTOR(n)`` -> ``'{n}f'`` (``4n`` bytes: ``n`` componentes float32)

``POINT`` y ``VECTOR`` se guardan dentro del registro como un campo de bytes de
longitud fija (``'16s'`` / ``'{4n}s'``) y se convierten al codificar/decodificar,
de modo que el registro sigue siendo de longitud fija.

Ejemplo: ``empleados(id INT, nombre CHAR(30), dept CHAR(20), salario FLOAT)`` se
empaqueta como ``'<i30s20sd'`` = 62 bytes.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from enum import Enum
from typing import Any, Sequence

INT_MIN = -(2**31)
INT_MAX = 2**31 - 1
MAX_CHAR_SIZE = 4000  # TEXT(n): documentos cortos dentro del registro
MAX_VECTOR_DIM = 1000
_POINT = struct.Struct("<dd")


class SchemaError(Exception):
    """Definición de esquema inválida o valor incompatible con una columna."""


class ColumnType(str, Enum):
    INT = "INT"
    FLOAT = "FLOAT"
    CHAR = "CHAR"
    POINT = "POINT"
    VECTOR = "VECTOR"


TYPE_CODES = {
    ColumnType.INT: 0,
    ColumnType.FLOAT: 1,
    ColumnType.CHAR: 2,
    ColumnType.POINT: 3,
    ColumnType.VECTOR: 4,
}
CODE_TYPES = {code: t for t, code in TYPE_CODES.items()}


def encode_str(value: str, size: int) -> bytes:
    """UTF-8 truncado a ``size`` bytes sin dejar un carácter multibyte a medias."""
    raw = value.encode("utf-8")
    if len(raw) > size:
        raw = raw[:size].decode("utf-8", errors="ignore").encode("utf-8")
    return raw


def decode_str(raw: bytes) -> str:
    return raw.rstrip(b"\x00").decode("utf-8", errors="ignore")


def _numbers(text: str) -> list[float]:
    """Números de un literal ``'[a, b, ...]'`` / ``'(a, b)'`` / ``'a b'``."""
    cleaned = text.strip()
    if cleaned.upper().startswith("POINT"):
        cleaned = cleaned[5:]
    cleaned = cleaned.strip().strip("[]()").replace(",", " ").replace(";", " ")
    return [float(tok) for tok in cleaned.split()]


def parse_point(value: Any) -> tuple[float, float]:
    """Acepta ``(x, y)``, ``[x, y]`` o un string ``'(x, y)'`` / ``'x,y'``."""
    if isinstance(value, str):
        try:
            value = _numbers(value)
        except ValueError:
            raise SchemaError(f"punto mal formado: {value!r}") from None
    if not isinstance(value, (tuple, list)) or len(value) != 2:
        raise SchemaError(f"un POINT necesita dos coordenadas (x, y) y recibió {value!r}")
    x, y = value
    for c in (x, y):
        if isinstance(c, bool) or not isinstance(c, (int, float)) or math.isnan(c) or math.isinf(c):
            raise SchemaError(f"coordenada inválida en el punto: {c!r}")
    return float(x), float(y)


def parse_vector(value: Any, dim: int | None = None) -> tuple[float, ...]:
    """Acepta una secuencia de números o un string ``'[0.1, -0.3, ...]'``."""
    if isinstance(value, str):
        try:
            value = _numbers(value)
        except ValueError:
            raise SchemaError("vector mal formado (se esperaba '[v1, v2, ...]')") from None
    if not isinstance(value, (tuple, list)) or not value:
        raise SchemaError(f"un VECTOR necesita una lista de números y recibió {value!r}")
    if dim is not None and len(value) != dim:
        raise SchemaError(f"el vector tiene {len(value)} componentes y la columna es VECTOR({dim})")
    out = []
    for c in value:
        if isinstance(c, bool) or not isinstance(c, (int, float)) or math.isnan(c) or math.isinf(c):
            raise SchemaError(f"componente inválido en el vector: {c!r}")
        out.append(float(c))
    return tuple(out)


@dataclass(frozen=True)
class Column:
    name: str
    type: ColumnType
    size: int | None = None
    primary_key: bool = False

    def __post_init__(self) -> None:
        if self.type == ColumnType.CHAR:
            if self.size is None or not 1 <= self.size <= MAX_CHAR_SIZE:
                raise SchemaError(f"CHAR({self.size}) inválido en '{self.name}' (1..{MAX_CHAR_SIZE})")
        elif self.type == ColumnType.VECTOR:
            if self.size is None or not 1 <= self.size <= MAX_VECTOR_DIM:
                raise SchemaError(f"VECTOR({self.size}) inválido en '{self.name}' (1..{MAX_VECTOR_DIM})")
            if self.primary_key:
                raise SchemaError(f"una columna VECTOR no puede ser PRIMARY KEY ('{self.name}')")
        elif self.size is not None:
            raise SchemaError(f"{self.type.value} no lleva tamaño ('{self.name}')")
        elif self.type == ColumnType.POINT and self.primary_key:
            raise SchemaError(f"una columna POINT no puede ser PRIMARY KEY ('{self.name}')")

    @property
    def fmt(self) -> str:
        if self.type == ColumnType.INT:
            return "i"
        if self.type == ColumnType.FLOAT:
            return "d"
        if self.type == ColumnType.POINT:
            return "16s"
        if self.type == ColumnType.VECTOR:
            return f"{4 * (self.size or 0)}s"
        return f"{self.size}s"

    @property
    def is_scalar(self) -> bool:
        """INT, FLOAT o CHAR: admite comparaciones e índices B+/hash."""
        return self.type in (ColumnType.INT, ColumnType.FLOAT, ColumnType.CHAR)

    @property
    def byte_size(self) -> int:
        return struct.calcsize("<" + self.fmt)

    def display_type(self) -> str:
        if self.type in (ColumnType.CHAR, ColumnType.VECTOR):
            return f"{self.type.value}({self.size})"
        return self.type.value

    def coerce(self, value: Any) -> Any:
        """Valida y convierte un literal Python al tipo de la columna."""
        if self.type == ColumnType.INT:
            if isinstance(value, bool):
                raise SchemaError(f"'{self.name}' es INT y recibió un booleano")
            if isinstance(value, float):
                if not value.is_integer():
                    raise SchemaError(f"'{self.name}' es INT y recibió {value}")
                value = int(value)
            if not isinstance(value, int):
                raise SchemaError(f"'{self.name}' es INT y recibió {value!r}")
            if not INT_MIN <= value <= INT_MAX:
                raise SchemaError(f"'{self.name}': {value} fuera del rango de INT")
            return value
        if self.type == ColumnType.FLOAT:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise SchemaError(f"'{self.name}' es FLOAT y recibió {value!r}")
            value = float(value)
            if math.isnan(value) or math.isinf(value):
                raise SchemaError(f"'{self.name}': FLOAT no admite NaN ni infinito")
            return 0.0 if value == 0.0 else value  # normaliza -0.0
        if self.type == ColumnType.POINT:
            return parse_point(value)
        if self.type == ColumnType.VECTOR:
            # se normaliza a float32, que es lo que queda en disco
            vec = parse_vector(value, self.size)
            fmt = f"<{len(vec)}f"
            try:
                return struct.unpack(fmt, struct.pack(fmt, *vec))
            except (struct.error, OverflowError):
                raise SchemaError(f"'{self.name}': componente fuera del rango de float32") from None
        if not isinstance(value, str):
            raise SchemaError(f"'{self.name}' es {self.display_type()} y recibió {value!r}")
        # Se normaliza al valor que realmente queda en disco (truncado por bytes).
        return encode_str(value, self.size or 0).decode("utf-8")

    def comparable(self, value: Any) -> Any:
        """Convierte un literal de un predicado al dominio de la columna para comparar.

        A diferencia de ``coerce`` acepta floats en columnas INT (``id < 10.5``).
        """
        if self.type == ColumnType.INT:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise SchemaError(f"no se puede comparar la columna INT '{self.name}' con {value!r}")
            return int(value) if isinstance(value, float) and value.is_integer() else value
        if self.type == ColumnType.FLOAT:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise SchemaError(f"no se puede comparar la columna FLOAT '{self.name}' con {value!r}")
            return float(value)
        if not self.is_scalar:
            raise SchemaError(
                f"la columna {self.display_type()} '{self.name}' no admite comparaciones (=, <, >, BETWEEN)"
            )
        if not isinstance(value, str):
            raise SchemaError(f"no se puede comparar la columna {self.display_type()} '{self.name}' con {value!r}")
        return encode_str(value, self.size or 0).decode("utf-8")


class Schema:
    """Lista ordenada de columnas y su ``struct`` de registro."""

    def __init__(self, columns: Sequence[Column]) -> None:
        if not columns:
            raise SchemaError("la tabla debe tener al menos una columna")
        names = [c.name for c in columns]
        if len(set(names)) != len(names):
            raise SchemaError("nombres de columna repetidos")
        pks = [i for i, c in enumerate(columns) if c.primary_key]
        if len(pks) > 1:
            raise SchemaError("solo se admite una columna PRIMARY KEY")
        self.columns: tuple[Column, ...] = tuple(columns)
        self.names: tuple[str, ...] = tuple(names)
        self._index = {name: i for i, name in enumerate(names)}
        self.pk_index: int | None = pks[0] if pks else None
        self.struct = struct.Struct("<" + "".join(c.fmt for c in columns))
        self.record_size = self.struct.size
        self.offsets: tuple[int, ...] = tuple(
            struct.calcsize("<" + "".join(c.fmt for c in columns[:i])) for i in range(len(columns))
        )
        self._char_positions = tuple(i for i, c in enumerate(columns) if c.type == ColumnType.CHAR)
        self._point_positions = tuple(i for i, c in enumerate(columns) if c.type == ColumnType.POINT)
        self._vector_positions = tuple(i for i, c in enumerate(columns) if c.type == ColumnType.VECTOR)
        self._vector_structs = {i: struct.Struct(f"<{columns[i].size}f") for i in self._vector_positions}
        self._converted = bool(self._char_positions or self._point_positions or self._vector_positions)

    def __len__(self) -> int:
        return len(self.columns)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Schema) and self.columns == other.columns

    def index_of(self, name: str) -> int:
        try:
            return self._index[name]
        except KeyError:
            raise SchemaError(f"no existe la columna '{name}'") from None

    def has_column(self, name: str) -> bool:
        return name in self._index

    def column(self, name: str) -> Column:
        return self.columns[self.index_of(name)]

    @property
    def primary_key(self) -> Column | None:
        return None if self.pk_index is None else self.columns[self.pk_index]

    def coerce(self, values: Sequence[Any]) -> tuple[Any, ...]:
        if len(values) != len(self.columns):
            raise SchemaError(f"se esperaban {len(self.columns)} valores y se recibieron {len(values)}")
        return tuple(col.coerce(v) for col, v in zip(self.columns, values))

    def encode(self, values: Sequence[Any]) -> bytes:
        """Empaqueta valores ya validados con ``coerce``."""
        if self._converted:
            vals = list(values)
            for i in self._char_positions:
                vals[i] = encode_str(vals[i], self.columns[i].size or 0)
            for i in self._point_positions:
                vals[i] = _POINT.pack(*vals[i])
            for i in self._vector_positions:
                vals[i] = self._vector_structs[i].pack(*vals[i])
            return self.struct.pack(*vals)
        return self.struct.pack(*values)

    def decode(self, data: bytes | bytearray | memoryview, offset: int = 0) -> tuple[Any, ...]:
        values = self.struct.unpack_from(data, offset)
        if not self._converted:
            return values
        vals = list(values)
        for i in self._char_positions:
            vals[i] = decode_str(vals[i])
        for i in self._point_positions:
            vals[i] = _POINT.unpack(vals[i])
        for i in self._vector_positions:
            vals[i] = self._vector_structs[i].unpack(vals[i])
        return tuple(vals)

    def raw_field(self, data: bytes | bytearray | memoryview, offset: int, col_index: int) -> bytes:
        """Bytes crudos de una columna dentro de un registro (sin decodificar el resto)."""
        start = offset + self.offsets[col_index]
        return bytes(data[start : start + self.columns[col_index].byte_size])


class KeyCodec:
    """Serialización de una clave de índice (el valor de una columna)."""

    __slots__ = ("column", "struct", "size", "is_char")

    def __init__(self, column: Column) -> None:
        self.column = column
        self.struct = struct.Struct("<" + column.fmt)
        self.size = self.struct.size
        self.is_char = column.type == ColumnType.CHAR

    @property
    def type_code(self) -> int:
        return TYPE_CODES[self.column.type]

    def encode(self, value: Any) -> bytes:
        if self.is_char:
            return self.struct.pack(encode_str(value, self.size))
        return self.struct.pack(value)

    def decode(self, data: bytes | bytearray | memoryview, offset: int = 0) -> Any:
        (value,) = self.struct.unpack_from(data, offset)
        return decode_str(value) if self.is_char else value

    def decode_raw(self, value: Any) -> Any:
        """Convierte el resultado crudo de ``struct.unpack`` al valor Python."""
        return decode_str(value) if self.is_char else value

    def normalize(self, value: Any) -> Any:
        """Lleva un literal al dominio de la clave (igual que se guarda en disco)."""
        return self.column.comparable(value)
