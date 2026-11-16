"""Copia de trabajo del catálogo de productos para los experimentos 7 y 8.

Los experimentos no tocan la base de la aplicación (``data/``): cargan
``data/productos.csv`` en una base nueva dentro de ``benchmarks/work/`` y crean
solo los índices que necesitan.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import numpy as np

from backend.database import Database
from benchmarks.common import ROOT, fresh_dir

DATA = ROOT / "data"
FEATURES = DATA / "raw" / "fashion" / "features.npz"
CREATE_TABLE = """CREATE TABLE productos (
  id INT PRIMARY KEY, titulo CHAR(64), categoria CHAR(16), tipo CHAR(24), genero CHAR(8), color CHAR(16),
  precio FLOAT, tienda CHAR(24), ubicacion POINT, descripcion TEXT(200), imagen CHAR(16),
  palabras TEXT(800), embedding VECTOR(128)
) USING HEAP"""
INDEXES = {
    "id": "CREATE INDEX idx_prod_id ON productos (id) USING HASH",
    "bovw": "CREATE INDEX idx_prod_bovw ON productos (palabras) USING INVERTED WITH (scoring = 'TFIDF_COSINE', analyzer = 'raw')",
    "vec": "CREATE INDEX idx_prod_vec ON productos (embedding) USING IVF WITH (metric = 'COSINE')",
}


def require_dataset() -> None:
    if not (DATA / "productos.csv").exists() or not FEATURES.exists():
        raise SystemExit("falta el catálogo: ejecute primero `python data/prepare_fashion.py`")


def load_features() -> dict[str, Any]:
    """Arreglos guardados por ``data/prepare_fashion.py`` (ids, embeddings, tipo, tiempos de extracción)."""
    require_dataset()
    with np.load(FEATURES) as z:
        return {k: z[k] for k in z.files}


def open_catalog(name: str, indexes: tuple[str, ...], limit: int | None = None) -> tuple[Database, dict[str, float]]:
    """Crea la base de trabajo, carga el CSV y construye los índices pedidos; devuelve sus tiempos."""
    require_dataset()
    work = fresh_dir(name)
    source = DATA / "productos.csv"
    if limit is None:
        shutil.copyfile(source, work / "productos.csv")
    else:
        with open(source, encoding="utf-8") as src, open(work / "productos.csv", "w", encoding="utf-8") as dst:
            for i, line in enumerate(src):
                if i > limit:
                    break
                dst.write(line)
    db = Database(work)
    db.execute(CREATE_TABLE)
    db.execute("COPY productos FROM 'productos.csv'")
    (work / "productos.csv").unlink()
    build: dict[str, float] = {}
    for key in indexes:
        out = db.execute(INDEXES[key])
        build[key] = out["metrics"]["exec_ms"] / 1000
    return db, build


def vector_literal(vector: Any) -> str:
    return "[" + ", ".join(f"{float(x):.5f}" for x in vector) + "]"
