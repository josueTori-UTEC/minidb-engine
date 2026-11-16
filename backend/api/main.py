"""API REST de MiniDB (FastAPI).

Endpoints principales (contrato completo en ``docs/api.md``):

* ``POST /api/query``: ejecuta SQL y devuelve filas paginadas, plan y métricas.
* ``GET /api/tables``: tablas, columnas, organización, estadísticas e índices.
* ``POST /api/tables/reorganize``: reorganiza una tabla SEQUENTIAL.
* ``GET /api/tables/{name}/files`` y ``GET /api/tables/{name}/pages/{page_id}``:
  inspección de páginas binarias (header decodificado + hexdump).
* ``GET /api/app/status``, ``GET /api/app/samples`` y ``POST /api/app/recommend``:
  aplicación de IA (recomendador multimodal); ``GET /api/media/{archivo}`` sirve
  las imágenes del catálogo.

La base de datos se abre al arrancar (``MINIDB_DATA_DIR``) y se cierra al
apagar, persistiendo las páginas 0 de todos los archivos.
"""

from __future__ import annotations

import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Literal

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from backend.app.recommender import Recommender, RecommenderError, RecommendRequest
from backend.config import get_settings
from backend.database import Database
from backend.sql.errors import SemanticError, SQLSyntaxError
from backend.storage.disk_manager import DEFAULT_PAGE_SIZE, DiskError
from backend.tools.dump_page import inspect_page


class QueryRequest(BaseModel):
    sql: str = Field(..., description="Una o varias sentencias separadas por ';'")
    page: int = Field(1, ge=1)
    page_size: int = Field(50, ge=1, le=1000)
    planner: Literal["rules", "cost"] | None = Field(
        None, description="'rules' (reglas del enunciado, por defecto) o 'cost' (menor costo estimado)"
    )


class ReorganizeRequest(BaseModel):
    table: str


def _error(exc: Exception, status: int = 400) -> HTTPException:
    if isinstance(exc, SQLSyntaxError):
        detail: dict[str, Any] = {
            "error": "SyntaxError",
            "message": exc.message,
            "position": exc.position.as_dict() if exc.position else None,
        }
    else:
        detail = {"error": "SemanticError", "message": getattr(exc, "message", str(exc)), "position": None}
    executed = getattr(exc, "executed", None)
    if executed:
        detail["executed"] = executed
    return HTTPException(status_code=status, detail=detail)


def create_app(data_dir: str | os.PathLike[str] | None = None) -> FastAPI:
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        path = Path(data_dir) if data_dir is not None else get_settings().data_dir
        app.state.db = Database(
            path,
            page_size=int(os.environ.get("MINIDB_PAGE_SIZE", DEFAULT_PAGE_SIZE)),
            planner_mode=os.environ.get("MINIDB_PLANNER", "rules"),
        )
        app.state.recommender = Recommender(app.state.db)
        if os.environ.get("MINIDB_WARM_UP", "1") == "1":
            threading.Thread(target=app.state.recommender.warm_up, daemon=True).start()
        try:
            yield
        finally:
            app.state.db.close()

    app = FastAPI(
        title="MiniDB API",
        version="1.0.0",
        description="Mini gestor de bases de datos relacional en disco (CS2042 UTEC)",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_methods=["*"],
        allow_headers=["*"],
    )

    def db_of(request: Request) -> Database:
        return request.app.state.db

    @app.get("/")
    def root() -> dict[str, str]:
        return {"message": "MiniDB API", "docs": "/docs"}

    @app.get("/health")
    @app.get("/api/health")
    def health(request: Request) -> dict[str, Any]:
        db = db_of(request)
        return {"status": "ok", "data_dir": str(db.data_dir), "tables": len(db.tables)}

    @app.post("/api/query")
    def query(body: QueryRequest, request: Request) -> dict[str, Any]:
        try:
            return db_of(request).execute(body.sql, body.page, body.page_size, body.planner)
        except (SQLSyntaxError, SemanticError) as exc:
            raise _error(exc) from None

    @app.get("/api/tables")
    def tables(request: Request) -> dict[str, Any]:
        return {"tables": db_of(request).tables_info()}

    @app.post("/api/tables/reorganize")
    def reorganize(body: ReorganizeRequest, request: Request) -> dict[str, Any]:
        db = db_of(request)
        if body.table not in db.tables:
            raise _error(SemanticError(f"no existe la tabla '{body.table}'"), 404)
        try:
            return db.reorganize(body.table)
        except SemanticError as exc:
            raise _error(exc) from None

    @app.get("/api/tables/{name}/files")
    def table_files(name: str, request: Request) -> dict[str, Any]:
        db = db_of(request)
        if name not in db.tables:
            raise _error(SemanticError(f"no existe la tabla '{name}'"), 404)
        return {"table": name, "files": db.table_files(name)}

    @app.get("/api/tables/{name}/pages/{page_id}")
    def page(name: str, page_id: int, request: Request, file: str = "data") -> dict[str, Any]:
        db = db_of(request)
        if name not in db.tables:
            raise _error(SemanticError(f"no existe la tabla '{name}'"), 404)
        try:
            with db._lock:
                table, path, key = db.file_path(name, file)
                table.sync()  # la página 0 en disco refleja el estado actual
                info = inspect_page(path, page_id, table.info.page_size, table.schema)
        except SemanticError as exc:
            raise _error(exc) from None
        except DiskError as exc:
            raise _error(SemanticError(str(exc))) from None
        return {"table": name, "file": key, **info}

    # ------------------------------------------------------------------ aplicación de IA
    def recommender_of(request: Request) -> Recommender:
        return request.app.state.recommender

    @app.get("/api/media/{filename}")
    def media(filename: str, request: Request) -> FileResponse:
        """Imagen de un producto (``<id>.jpg``) dentro de ``<MINIDB_DATA_DIR>/media``."""
        stem, _, ext = filename.partition(".")
        if not stem.isdigit() or ext != "jpg":
            raise HTTPException(status_code=404, detail="archivo no encontrado")
        path = recommender_of(request).media_dir / filename
        if not path.is_file():
            raise HTTPException(status_code=404, detail="archivo no encontrado")
        return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})

    @app.get("/api/app/status")
    def app_status(request: Request) -> dict[str, Any]:
        return recommender_of(request).status()

    @app.get("/api/app/samples")
    def app_samples(request: Request, n: int = 12, seed: int = 0) -> dict[str, Any]:
        """Productos de ejemplo para usar como imagen de consulta (por el índice de precio)."""
        db = db_of(request)
        if "productos" not in db.tables:
            return {"products": []}
        n = max(1, min(n, 48))
        products: list[dict[str, Any]] = []
        seen: set[int] = set()
        # el precio depende del tipo de artículo: varias ventanas de precio dan ejemplos variados
        try:
            for i in range(n * 3):
                if len(products) >= n:
                    break
                low = 25 + (seed * 37 + i * 53) % 340
                out = db.execute(
                    f"SELECT id, titulo, categoria, tipo, precio, imagen FROM productos "
                    f"WHERE precio >= {low} AND precio <= {low + 2} LIMIT 2", 1, 2,
                )
                for row in out["rows"]:
                    if row[0] not in seen and len(products) < n:
                        seen.add(row[0])
                        products.append(dict(zip(out["columns"], row)))
        except (SQLSyntaxError, SemanticError) as exc:
            raise _error(exc) from None
        return {"products": products}

    @app.post("/api/app/recommend")
    async def app_recommend(
        request: Request,
        image: UploadFile | None = File(None),
        product_id: int | None = Form(None),
        text: str = Form(""),
        price_min: float | None = Form(None),
        price_max: float | None = Form(None),
        lon: float | None = Form(None),
        lat: float | None = Form(None),
        radius_km: float | None = Form(None),
        category: str = Form(""),
        k: int = Form(12),
        probes: int = Form(5),
        strategy: str = Form("auto"),
        methods: str = Form(""),
    ) -> dict[str, Any]:
        """Recomienda productos parecidos a una imagen y/o a una descripción, con filtros de precio y cercanía."""
        rec = recommender_of(request)
        req = RecommendRequest(
            text=text, price_min=price_min, price_max=price_max, lon=lon, lat=lat, radius_km=radius_km,
            category=category, k=k, probes=probes, strategy=strategy,
            methods=[m.strip() for m in methods.split(",") if m.strip()],
        )
        try:
            data = None
            if image is not None:
                data = await image.read()
                if len(data) > 8 * 1024 * 1024:
                    raise RecommenderError("la imagen supera los 8 MB")
                if not data:
                    data = None
            if data is None and product_id is not None:
                data = rec.image_bytes(product_id)
            out = rec.recommend(req, data)
        except RecommenderError as exc:
            raise _error(SemanticError(str(exc))) from None
        out["query"]["product_id"] = product_id
        return out

    return app


app = create_app()
