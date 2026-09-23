"""API REST de MiniDB (FastAPI).

Endpoints principales (contrato completo en ``docs/api.md``):

* ``POST /api/query``: ejecuta SQL y devuelve filas paginadas, plan y métricas.
* ``GET /api/tables``: tablas, columnas, organización, estadísticas e índices.
* ``POST /api/tables/reorganize``: reorganiza una tabla SEQUENTIAL.
* ``GET /api/tables/{name}/files`` y ``GET /api/tables/{name}/pages/{page_id}``:
  inspección de páginas binarias (header decodificado + hexdump).

La base de datos se abre al arrancar (``MINIDB_DATA_DIR``) y se cierra al
apagar, persistiendo las páginas 0 de todos los archivos.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

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

    return app


app = create_app()
