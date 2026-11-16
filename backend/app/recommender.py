"""Aplicación de IA: recomendador multimodal de productos de moda (Opción B).

El usuario sube la foto de una prenda (o elige una del catálogo) y/o escribe una
descripción, y puede acotar por rango de precios y por cercanía a una tienda. La
aplicación **no accede a los archivos del motor**: arma sentencias SQL y las
ejecuta con ``Database.execute``, de modo que cada resultado trae su plan de
ejecución y sus lecturas de disco.

Para una imagen se comparan dos representaciones sobre la misma tabla
``productos``:

* **Embedding denso** (ResNet-18 -> PCA 128 D) con el índice IVF::

      SELECT ..., embedding <=> '[...]' AS distancia FROM productos
      WHERE precio BETWEEN a AND b AND ubicacion WITHIN_RADIUS((lon, lat), r)
      ORDER BY distancia ASC LIMIT k;

* **BoVW** (SIFT + codebook) con el índice invertido sobre ``palabras``::

      SELECT ..., COSINE_SCORE() AS similitud FROM productos
      WHERE palabras MATCH 'v12:3 v85 ...' AND precio BETWEEN a AND b AND ...
      ORDER BY similitud DESC LIMIT k;

y, opcionalmente, el k-NN exacto por recorrido secuencial como verdad base. Para
una descripción en texto se usa el índice invertido sobre ``descripcion``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from backend.database import Database
from backend.ml.features import FeatureError, FeatureExtractor
from backend.sql.errors import SemanticError, SQLSyntaxError

TABLE = "productos"
OUTPUT = "id, titulo, categoria, tipo, color, precio, tienda, ubicacion, imagen"
METHODS = {
    "embedding": "Embedding ResNet-18 + IVF",
    "bovw": "BoVW (SIFT) + índice invertido",
    "exact": "k-NN exacto (recorrido secuencial)",
    "text": "Texto: TF-IDF + coseno",
}


@dataclass
class RecommendRequest:
    text: str = ""
    price_min: float | None = None
    price_max: float | None = None
    lon: float | None = None
    lat: float | None = None
    radius_km: float | None = None
    category: str = ""
    k: int = 12
    probes: int = 5
    strategy: str = "auto"
    methods: list[str] = field(default_factory=list)


class RecommenderError(Exception):
    pass


def _quote(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _filters(req: RecommendRequest) -> list[str]:
    out = []
    if req.price_min is not None and req.price_max is not None:
        out.append(f"precio BETWEEN {float(req.price_min)} AND {float(req.price_max)}")
    elif req.price_min is not None:
        out.append(f"precio >= {float(req.price_min)}")
    elif req.price_max is not None:
        out.append(f"precio <= {float(req.price_max)}")
    if req.lon is not None and req.lat is not None and req.radius_km:
        out.append(f"ubicacion WITHIN_RADIUS(({float(req.lon)}, {float(req.lat)}), {float(req.radius_km)})")
    if req.category:
        out.append(f"categoria = {_quote(req.category.lower())}")
    return out


def _where(conditions: list[str]) -> str:
    return (" WHERE " + " AND ".join(conditions)) if conditions else ""


def _text_query(text: str) -> str:
    """Texto libre del usuario -> consulta MATCH (sin operadores ni comillas)."""
    words = [w for w in text.replace("(", " ").replace(")", " ").split() if w not in ("AND", "OR", "NOT")]
    return " ".join(words)


def build_queries(req: RecommendRequest, features: dict[str, Any] | None) -> list[dict[str, str]]:
    """SQL de cada método pedido: ``sql`` se ejecuta y ``display`` es la versión abreviada para mostrar."""
    k = max(1, min(int(req.k), 60))
    filters = [f.replace("%", "%%") for f in _filters(req)]  # se insertan en plantillas con %
    queries: list[dict[str, str]] = []
    methods = req.methods or (["embedding", "bovw"] if features else ["text"])
    for method in methods:
        if method not in METHODS:
            raise RecommenderError(f"método desconocido '{method}' (use {', '.join(METHODS)})")
        if method in ("embedding", "exact", "bovw") and features is None:
            raise RecommenderError(f"el método '{method}' necesita una imagen de consulta")
        if method in ("embedding", "exact"):
            vector = "[" + ", ".join(f"{x:.5f}" for x in features["embedding"]) + "]"  # type: ignore[index]
            op = "<=>" if method == "embedding" else "<->"  # el IVF es COSINE: con <-> se fuerza el recorrido exacto
            conditions = list(filters)
            if req.text.strip():
                conditions.append(f"descripcion MATCH {_quote(_text_query(req.text))}".replace("%", "%%"))
            template = (f"SELECT {OUTPUT}, embedding {op} '%s' AS distancia FROM {TABLE}{_where(conditions)} "
                        f"ORDER BY distancia ASC LIMIT {k}")
            head = ", ".join(f"{x:.2f}" for x in features["embedding"][:3])  # type: ignore[index]
            queries.append({"method": method, "sql": template % vector,
                            "display": template % f"[{head}, … (128 dims)]", "score": "distancia"})
        elif method == "bovw":
            words = features["words"]  # type: ignore[index]
            if not words:
                raise RecommenderError("la imagen no tiene puntos de interés SIFT (pruebe con otra foto)")
            conditions = [f"palabras MATCH '%s'", *filters]
            template = (f"SELECT {OUTPUT}, COSINE_SCORE() AS similitud FROM {TABLE}{_where(conditions)} "
                        f"ORDER BY similitud DESC LIMIT {k}")
            short = " ".join(words.split()[:6]) + f" … ({len(words.split())} palabras visuales)"
            queries.append({"method": method, "sql": template % words, "display": template % short,
                            "score": "similitud"})
        else:
            query = _text_query(req.text)
            if not query:
                raise RecommenderError("escriba una descripción para la búsqueda por texto")
            conditions = [f"descripcion MATCH {_quote(query)}", *_filters(req)]
            sql = (f"SELECT {OUTPUT}, COSINE_SCORE() AS relevancia FROM {TABLE}{_where(conditions)} "
                   f"ORDER BY relevancia DESC LIMIT {k}")
            queries.append({"method": method, "sql": sql, "display": sql, "score": "relevancia"})
    return queries


class Recommender:
    def __init__(self, db: Database) -> None:
        self.db = db
        self.extractor = FeatureExtractor(Path(db.data_dir) / "models")

    @property
    def media_dir(self) -> Path:
        return Path(self.db.data_dir) / "media"

    def warm_up(self) -> None:
        """Carga el stemmer y, si el catálogo está listo, la red y los modelos (evita la espera de la primera consulta)."""
        from backend.text.analyzer import Analyzer

        Analyzer("english").tokens("warm up")
        Analyzer("spanish").tokens("calentamiento")
        if TABLE in self.db.tables and self.extractor.available:
            sample = next(iter(sorted(self.media_dir.glob("*.jpg"))), None) if self.media_dir.is_dir() else None
            if sample is not None:
                try:
                    self.extractor.extract(sample.read_bytes())
                except Exception:  # la precarga es opcional: sin torch u OpenCV solo falla la búsqueda por imagen
                    pass

    def status(self) -> dict[str, Any]:
        table = self.db.tables.get(TABLE)
        info = None
        if table is not None:
            stats = table.stats()
            info = {
                "records": stats["record_count"],
                "pages": stats["page_count"],
                "indexes": [{"name": i["name"], "type": i["type"], "column": i["column"]} for i in stats["indexes"]],
            }
        return {
            "table": TABLE,
            "loaded": table is not None,
            "models": self.extractor.available,
            "media": self.media_dir.is_dir(),
            "info": info,
            "methods": METHODS,
        }

    def image_bytes(self, product_id: int) -> bytes:
        path = self.media_dir / f"{int(product_id)}.jpg"
        if not path.is_file():
            raise RecommenderError(f"no hay imagen para el producto {product_id}")
        return path.read_bytes()

    def recommend(self, req: RecommendRequest, image: bytes | None) -> dict[str, Any]:
        if TABLE not in self.db.tables:
            raise RecommenderError(
                "la tabla 'productos' no está cargada (ejecute: python data/prepare_fashion.py --load)"
            )
        if image is None and not req.text.strip():
            raise RecommenderError("suba una imagen, elija un producto o escriba una descripción")
        if req.strategy not in ("auto", "pre", "post"):
            raise RecommenderError("strategy debe ser auto, pre o post")
        features = None
        if image is not None:
            try:
                features = self.extractor.extract(image)
            except FeatureError as exc:
                raise RecommenderError(str(exc)) from None
        results = []
        setup = f"SET ivf.probes = {max(1, int(req.probes))}; SET hybrid.strategy = {req.strategy}; "
        for q in build_queries(req, features):
            entry: dict[str, Any] = {"method": q["method"], "label": METHODS[q["method"]], "sql": q["display"],
                                     "score_column": q["score"]}
            try:
                out = self.db.execute(setup + q["sql"], 1, 60)
                entry.update({"columns": out["columns"], "rows": out["rows"], "total_rows": out["total_rows"],
                              "plan": out["plan"], "metrics": out["metrics"], "error": None})
            except (SemanticError, SQLSyntaxError) as exc:
                entry.update({"columns": [], "rows": [], "total_rows": 0, "plan": None, "metrics": None,
                              "error": getattr(exc, "message", str(exc))})
            results.append(entry)
        self.db.execute("SET ivf.probes = 5; SET hybrid.strategy = auto")
        summary = None
        if features is not None:
            summary = {k: features[k] for k in ("keypoints", "embedding_ms", "bovw_ms", "width", "height")}
            summary["visual_words"] = len(features["words"].split())
        return {
            "query": {"text": req.text, "has_image": image is not None, "k": req.k, "probes": req.probes,
                      "strategy": req.strategy,
                      "filters": {"price_min": req.price_min, "price_max": req.price_max, "lon": req.lon,
                                  "lat": req.lat, "radius_km": req.radius_km, "category": req.category}},
            "features": summary,
            "results": results,
        }
