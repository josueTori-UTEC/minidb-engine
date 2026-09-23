"""Configuración del motor, leída desde variables de entorno.

- ``MINIDB_DATA_DIR``: carpeta donde viven el catálogo y los archivos binarios
  (por defecto ``<repo>/data``).
- ``MINIDB_CORS_ORIGINS``: orígenes permitidos para el frontend, separados por
  comas (``*`` permite cualquiera).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CORS_ORIGINS = "http://localhost:5173,http://127.0.0.1:5173"


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    cors_origins: tuple[str, ...]


def get_settings() -> Settings:
    data_dir = Path(os.environ.get("MINIDB_DATA_DIR", REPO_ROOT / "data"))
    origins = os.environ.get("MINIDB_CORS_ORIGINS", DEFAULT_CORS_ORIGINS)
    return Settings(
        data_dir=data_dir,
        cors_origins=tuple(o.strip() for o in origins.split(",") if o.strip()),
    )
