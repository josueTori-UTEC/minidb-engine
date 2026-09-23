"""Identificador de registro ``RID = (page_id, slot)``.

Se empaqueta como ``'<iH'`` (6 bytes). En un Sequential File un ``page_id``
negativo apunta al área de overflow: ``RID(-k, s)`` es el slot ``s`` de la página
``k`` del archivo ``.ovf`` (la página 0 es de metadatos, así que no hay ambigüedad).
"""

from __future__ import annotations

import struct
from typing import NamedTuple

RID_STRUCT = struct.Struct("<iH")
RID_SIZE = RID_STRUCT.size  # 6


class RID(NamedTuple):
    page_id: int
    slot: int

    def pack(self) -> bytes:
        return RID_STRUCT.pack(self.page_id, self.slot)

    @classmethod
    def unpack(cls, data: bytes | bytearray | memoryview, offset: int = 0) -> "RID":
        return cls(*RID_STRUCT.unpack_from(data, offset))

    @property
    def in_overflow(self) -> bool:
        return self.page_id < 0

    def __str__(self) -> str:
        return f"({self.page_id},{self.slot})"
