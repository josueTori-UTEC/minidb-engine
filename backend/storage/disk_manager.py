"""Acceso a disco por páginas completas con conteo exacto de I/O.

Todo el motor lee y escribe archivos exclusivamente a través de ``DiskManager``:
cada lectura es ``seek(page_id * page_size)`` + ``read(page_size)`` y cada
escritura es ``seek`` + ``write`` de una página completa. No hay buffer pool, así
que el ``DiskCounter`` refleja exactamente los bloques transferidos y se puede
comparar contra los costos teóricos.
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_PAGE_SIZE = 4096
# Tamaños usados en el Experimento 4. Se aceptan potencias de 2 entre 512 y 65536
# (los tests usan 512 para obtener árboles profundos con pocos datos).
EXPERIMENT_PAGE_SIZES = (1024, 2048, 4096, 8192)
MIN_PAGE_SIZE = 512
MAX_PAGE_SIZE = 65536


class DiskCounter:
    """Monitor de I/O: bloques físicos leídos (``disk_reads``) y escritos (``disk_writes``).

    Se comparte entre todos los ``DiskManager`` de una ejecución y se resetea antes
    de cada consulta.
    """

    __slots__ = ("disk_reads", "disk_writes")

    def __init__(self) -> None:
        self.disk_reads = 0
        self.disk_writes = 0

    def reset(self) -> None:
        self.disk_reads = 0
        self.disk_writes = 0

    def snapshot(self) -> tuple[int, int]:
        return self.disk_reads, self.disk_writes

    def as_dict(self) -> dict[str, int]:
        return {"disk_reads": self.disk_reads, "disk_writes": self.disk_writes}

    def __repr__(self) -> str:
        return f"DiskCounter(reads={self.disk_reads}, writes={self.disk_writes})"


class DiskError(Exception):
    """Error de acceso a páginas (id fuera de rango, tamaño inválido, archivo corrupto)."""


def validate_page_size(page_size: int) -> int:
    if (
        not isinstance(page_size, int)
        or page_size < MIN_PAGE_SIZE
        or page_size > MAX_PAGE_SIZE
        or page_size & (page_size - 1)
    ):
        raise DiskError(
            f"page_size inválido: {page_size} (potencia de 2 entre {MIN_PAGE_SIZE} y {MAX_PAGE_SIZE})"
        )
    return page_size


class DiskManager:
    """Un archivo binario dividido en páginas de ``page_size`` bytes.

    El archivo se abre una sola vez en modo ``r+b`` sin buffer de Python
    (``buffering=0``), de modo que cada ``read_page``/``write_page`` es exactamente
    una transferencia de un bloque.
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        page_size: int,
        counter: DiskCounter,
        *,
        create: bool = False,
    ) -> None:
        self.path = Path(path)
        self.page_size = validate_page_size(page_size)
        self.counter = counter
        if create:
            # "xb" falla si el archivo ya existe: nunca se pisa un archivo existente.
            with open(self.path, "xb"):
                pass
        self._file = open(self.path, "r+b", buffering=0)
        size = os.fstat(self._file.fileno()).st_size
        if size % self.page_size != 0:
            self._file.close()
            raise DiskError(
                f"{self.path.name}: tamaño {size} no es múltiplo de page_size={self.page_size}"
            )
        self._num_pages = size // self.page_size

    # ------------------------------------------------------------------ páginas
    def read_page(self, page_id: int) -> bytes:
        """Lee la página ``page_id`` completa (1 lectura)."""
        if not 0 <= page_id < self._num_pages:
            raise DiskError(f"{self.path.name}: página {page_id} fuera de rango (0..{self._num_pages - 1})")
        self._file.seek(page_id * self.page_size)
        data = self._file.read(self.page_size)
        if data is None or len(data) != self.page_size:
            raise DiskError(f"{self.path.name}: lectura incompleta de la página {page_id}")
        self.counter.disk_reads += 1
        return data

    def write_page(self, page_id: int, data: bytes | bytearray | memoryview) -> None:
        """Escribe la página ``page_id`` completa (1 escritura)."""
        if len(data) != self.page_size:
            raise DiskError(f"{self.path.name}: se intentó escribir {len(data)} bytes (page_size={self.page_size})")
        if not 0 <= page_id < self._num_pages:
            raise DiskError(f"{self.path.name}: página {page_id} no asignada (num_pages={self._num_pages})")
        self._file.seek(page_id * self.page_size)
        view = memoryview(data)
        written = 0
        while written < self.page_size:
            n = self._file.write(view[written:])
            if not n:
                raise DiskError(f"{self.path.name}: escritura incompleta de la página {page_id}")
            written += n
        self.counter.disk_writes += 1

    def allocate_page(self) -> int:
        """Reserva el siguiente ``page_id`` al final del archivo.

        No hace I/O: el llamador escribe el contenido inicial con ``write_page``
        (esa escritura es la que se cuenta), así agregar una página cuesta 1W.
        """
        page_id = self._num_pages
        self._num_pages += 1
        return page_id

    def num_pages(self) -> int:
        return self._num_pages

    def truncate(self, num_pages: int) -> None:
        """Recorta el archivo a ``num_pages`` páginas (no transfiere bloques)."""
        if not 0 <= num_pages <= self._num_pages:
            raise DiskError(f"{self.path.name}: truncate({num_pages}) inválido")
        self._file.truncate(num_pages * self.page_size)
        self._num_pages = num_pages

    def file_size(self) -> int:
        return self._num_pages * self.page_size

    # ------------------------------------------------------------------ ciclo de vida
    def flush(self, *, sync: bool = False) -> None:
        self._file.flush()
        if sync:
            os.fsync(self._file.fileno())

    def close(self) -> None:
        if not self._file.closed:
            self._file.close()

    @property
    def closed(self) -> bool:
        return self._file.closed

    def __enter__(self) -> DiskManager:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"DiskManager({self.path.name!r}, page_size={self.page_size}, pages={self._num_pages})"
