"""Prepara el catálogo de moda para la aplicación de IA (recomendador multimodal).

Dataset: *Fashion Product Images (Small)* — 44 072 productos con imagen (60 × 80),
nombre, género, categoría, tipo de artículo, color, temporada y uso. El dataset no
trae precio ni tienda, así que se generan de forma determinista (semilla fija):
un precio en soles según el tipo de artículo y una tienda con coordenadas dentro
de Lima Metropolitana.

Pasos (``python data/prepare_fashion.py``):

1. descarga los dos archivos Parquet del dataset (si no están en ``data/raw/fashion``);
2. extrae las imágenes a ``data/media/<id>.jpg``;
3. **embeddings**: ResNet-18 (512 D) -> PCA a 128 D -> normalización L2;
4. **BoVW**: SIFT (hasta 100 descriptores por imagen), *codebook* de ``K`` palabras
   visuales con K-Means sobre una muestra de descriptores, histograma por imagen;
5. escribe ``data/productos.csv`` y los modelos en ``data/models/``;
6. con ``--load`` crea la tabla ``productos`` en el motor, la carga con ``COPY`` y
   construye los índices (hash, B+, R-Tree, invertidos e IVF).

La carga también se puede hacer desde el cliente web ejecutando ``data/fashion.sql``.
"""

from __future__ import annotations

import argparse
import csv
import io
import sys
import time
import urllib.request
import zlib
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.indexes.ivf import kmeans  # noqa: E402
from backend.ml import features as F  # noqa: E402

DATA = Path(__file__).resolve().parent
RAW = DATA / "raw" / "fashion"
MEDIA = DATA / "media"
MODELS = DATA / "models"
BASE_URL = "https://huggingface.co/datasets/ashraq/fashion-product-images-small/resolve/main/data/"
PARQUET_FILES = (
    "train-00000-of-00002-6cff4c59f91661c3.parquet",
    "train-00001-of-00002-bb459e5ac5f01e71.parquet",
)
CODEBOOK_SIZE = 1000
CODEBOOK_SAMPLE = 200_000
BATCH = 256

# Centro (longitud, latitud) de distritos de Lima Metropolitana
DISTRICTS = [
    ("Miraflores", -77.0282, -12.1211), ("San Isidro", -77.0365, -12.0977), ("Barranco", -77.0219, -12.1494),
    ("Surco", -76.9930, -12.1450), ("San Borja", -77.0010, -12.1070), ("La Molina", -76.9420, -12.0800),
    ("Lince", -77.0360, -12.0850), ("Jesus Maria", -77.0490, -12.0770), ("Pueblo Libre", -77.0650, -12.0750),
    ("San Miguel", -77.0870, -12.0770), ("Magdalena", -77.0720, -12.0910), ("Cercado", -77.0428, -12.0464),
    ("Brena", -77.0530, -12.0580), ("La Victoria", -77.0170, -12.0680), ("Surquillo", -77.0170, -12.1130),
    ("Chorrillos", -77.0230, -12.1740), ("SJM", -76.9710, -12.1570), ("Villa El Salvador", -76.9390, -12.2130),
    ("Ate", -76.9190, -12.0260), ("Santa Anita", -76.9710, -12.0430), ("SJL", -77.0000, -11.9830),
    ("Los Olivos", -77.0700, -11.9580), ("Independencia", -77.0540, -11.9930), ("Comas", -77.0500, -11.9370),
    ("SMP", -77.0850, -12.0050), ("Callao", -77.1180, -12.0560), ("Rimac", -77.0290, -12.0290),
    ("El Agustino", -76.9970, -12.0430),
]
STORES_PER_DISTRICT = 5

COLUMNS = ["id", "titulo", "categoria", "tipo", "genero", "color", "precio", "tienda", "ubicacion",
           "descripcion", "imagen", "palabras", "embedding"]

SCHEMA_SQL = """CREATE TABLE productos (
  id INT PRIMARY KEY, titulo CHAR(64), categoria CHAR(16), tipo CHAR(24), genero CHAR(8), color CHAR(16),
  precio FLOAT, tienda CHAR(24), ubicacion POINT, descripcion TEXT(200), imagen CHAR(16),
  palabras TEXT(800), embedding VECTOR(128)
) USING HEAP;
COPY productos FROM 'productos.csv';
CREATE INDEX idx_prod_id ON productos (id) USING HASH;
CREATE INDEX idx_prod_precio ON productos (precio) USING BTREE;
CREATE INDEX idx_prod_geo ON productos (ubicacion) USING RTREE;
CREATE INDEX idx_prod_texto ON productos (descripcion) USING INVERTED WITH (scoring = 'TFIDF_COSINE', analyzer = 'english');
CREATE INDEX idx_prod_bovw ON productos (palabras) USING INVERTED WITH (scoring = 'TFIDF_COSINE', analyzer = 'raw');
CREATE INDEX idx_prod_vec ON productos (embedding) USING IVF WITH (metric = 'COSINE');
"""


def unit(seed: str) -> float:
    """Pseudoaleatorio determinista en [0, 1) a partir de un texto."""
    return (zlib.crc32(seed.encode("utf-8")) & 0xFFFFFFFF) / 2**32


def stores() -> list[tuple[str, float, float]]:
    out = []
    for name, lon, lat in DISTRICTS:
        for i in range(STORES_PER_DISTRICT):
            dx = (unit(f"{name}-x-{i}") - 0.5) * 0.024
            dy = (unit(f"{name}-y-{i}") - 0.5) * 0.024
            out.append((f"{name} {i + 1}", round(lon + dx, 6), round(lat + dy, 6)))
    return out


def price(article: str, product_id: int) -> float:
    base = 25 + 375 * unit("base-" + article)
    return round(base * (0.6 + 0.8 * unit(f"precio-{product_id}")), 2)


def clean(text: object) -> str:
    return " ".join(str(text or "").replace(",", " ").replace('"', " ").split())


def download() -> list[Path]:
    RAW.mkdir(parents=True, exist_ok=True)
    paths = []
    for name in PARQUET_FILES:
        path = RAW / name
        if not path.exists():
            print(f"descargando {name} ...", flush=True)
            urllib.request.urlretrieve(BASE_URL + name, path)
        paths.append(path)
    return paths


def records(paths: list[Path], limit: int | None):
    import pyarrow.parquet as pq

    count = 0
    for path in paths:
        for batch in pq.ParquetFile(path).iter_batches(batch_size=BATCH):
            for row in batch.to_pylist():
                yield row
                count += 1
                if limit is not None and count >= limit:
                    return


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepara el catálogo de moda (imágenes, embeddings, BoVW y CSV).")
    parser.add_argument("--limit", type=int, default=None, help="procesar solo los primeros N productos")
    parser.add_argument("--codebook", type=int, default=CODEBOOK_SIZE, help="palabras visuales del codebook")
    parser.add_argument("--load", action="store_true", help="crear la tabla, cargarla y construir los índices")
    parser.add_argument("--only-load", action="store_true", help="omitir la extracción y solo cargar el CSV")
    args = parser.parse_args()
    if not args.only_load:
        extract(args.limit, args.codebook)
    if args.load or args.only_load:
        load()


def extract(limit: int | None, codebook_size: int) -> None:
    t_start = time.perf_counter()
    paths = download()
    MEDIA.mkdir(parents=True, exist_ok=True)
    MODELS.mkdir(parents=True, exist_ok=True)
    shops = stores()

    ids: list[int] = []
    meta: list[list[object]] = []
    resnet: list[np.ndarray] = []
    descriptors: list[np.ndarray] = []
    seen: set[int] = set()
    batch_images = []
    t_resnet = t_sift = 0.0

    def flush_batch() -> None:
        nonlocal t_resnet
        if batch_images:
            t0 = time.perf_counter()
            resnet.append(F.resnet_features(batch_images))
            t_resnet += time.perf_counter() - t0
            batch_images.clear()

    for row in records(paths, limit):
        pid = int(row["id"])
        if pid in seen:
            continue
        seen.add(pid)
        raw = row["image"]["bytes"]
        try:
            image = F.load_image(raw)
        except F.FeatureError:
            continue
        (MEDIA / f"{pid}.jpg").write_bytes(raw if raw[:2] == b"\xff\xd8" else _to_jpeg(image))
        t0 = time.perf_counter()
        descriptors.append(F.sift_descriptors(image))
        t_sift += time.perf_counter() - t0
        batch_images.append(image)
        if len(batch_images) == BATCH:
            flush_batch()
        article = clean(row["articleType"])
        shop = shops[zlib.crc32(f"tienda-{pid}".encode()) % len(shops)]
        year = "" if row["year"] is None else str(int(row["year"]))
        title = clean(row["productDisplayName"]) or f"{clean(row['baseColour'])} {article}"
        description = (
            f"{title}. {clean(row['gender'])} {clean(row['baseColour'])} {article} for {clean(row['usage'])} wear "
            f"{clean(row['season'])} {year} collection. {clean(row['masterCategory'])} {clean(row['subCategory'])}"
        )
        ids.append(pid)
        meta.append([
            pid, title[:64], clean(row["masterCategory"]).lower()[:16], article[:24], clean(row["gender"])[:8],
            clean(row["baseColour"])[:16], price(article, pid), shop[0][:24], f"({shop[1]}, {shop[2]})",
            description[:200], f"{pid}.jpg",
        ])
        if len(ids) % 5000 == 0:
            print(f"  {len(ids)} imágenes procesadas ({time.perf_counter() - t_start:.0f} s)", flush=True)
    flush_batch()
    n = len(ids)
    print(f"{n} productos · ResNet-18: {t_resnet:.1f} s · SIFT: {t_sift:.1f} s", flush=True)

    # --- embeddings: PCA 512 -> 128 y normalización
    feats = np.vstack(resnet)
    pca = F.fit_pca(feats, F.EMBEDDING_DIM)
    embeddings = F.project(feats, pca)
    np.savez(MODELS / F.PCA_FILE, **pca)
    print(f"PCA: {F.RESNET_DIM} -> {F.EMBEDDING_DIM} dims, varianza explicada {float(pca['explained'][0]):.1%}", flush=True)

    # --- BoVW: codebook con K-Means sobre una muestra de descriptores
    total_desc = sum(len(d) for d in descriptors)
    rng = np.random.default_rng(42)
    stacked = np.vstack([d for d in descriptors if len(d)])
    sample = stacked if len(stacked) <= CODEBOOK_SAMPLE else stacked[rng.choice(len(stacked), CODEBOOK_SAMPLE, replace=False)]
    t0 = time.perf_counter()
    codebook = kmeans(sample, codebook_size, "L2", iterations=15)
    t_kmeans = time.perf_counter() - t0
    np.savez(MODELS / F.CODEBOOK_FILE, codebook=codebook)
    del stacked
    print(f"codebook: {len(codebook)} palabras visuales con {len(sample)} de {total_desc} descriptores "
          f"({t_kmeans:.1f} s)", flush=True)
    t0 = time.perf_counter()
    words = [F.words_text(F.quantize(d, codebook)) for d in descriptors]
    t_quant = time.perf_counter() - t0

    # --- CSV para COPY
    with open(DATA / "productos.csv", "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(COLUMNS)
        for row, w, emb in zip(meta, words, embeddings):
            writer.writerow([*row, fit_words(w), "[" + " ".join(f"{x:.5f}" for x in emb) + "]"])
    (DATA / "fashion.sql").write_text(SCHEMA_SQL, encoding="utf-8")

    # --- artefactos para los experimentos 7 y 8
    np.savez(
        RAW / "features.npz",
        ids=np.asarray(ids, dtype=np.int64),
        embeddings=embeddings,
        resnet=feats,
        tipo=np.asarray([m[3] for m in meta]),
        categoria=np.asarray([m[2] for m in meta]),
        keypoints=np.asarray([len(d) for d in descriptors], dtype=np.int32),
        timing=np.asarray([t_resnet, t_sift, t_kmeans, t_quant], dtype=np.float64),
    )
    (RAW / "bovw_words.txt").write_text("\n".join(words), encoding="utf-8")
    print(f"listo en {time.perf_counter() - t_start:.0f} s: data/productos.csv ({n} filas), "
          f"data/media/ y data/models/", flush=True)


def fit_words(words: str, size: int = 800) -> str:
    """Recorta el histograma a ``size`` bytes sin partir una palabra visual."""
    return words if len(words) <= size else words[:size].rsplit(" ", 1)[0]


def _to_jpeg(image) -> bytes:
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def load() -> None:
    from backend.database import Database

    db = Database(DATA)
    try:
        if "productos" in db.tables:
            db.execute("DROP TABLE productos")
        for statement in [s.strip() for s in SCHEMA_SQL.split(";") if s.strip()]:
            t0 = time.perf_counter()
            out = db.execute(statement)
            m = out["metrics"]
            print(f"{out['message']}  [{time.perf_counter() - t0:.1f} s · {m['disk_reads']} R · {m['disk_writes']} W]",
                  flush=True)
    finally:
        db.close()


if __name__ == "__main__":
    main()
