from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.config import get_settings

settings = get_settings()

app = FastAPI(
    title="MiniDB API",
    version="0.1.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_origins),
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def root():
    return {
        "message": "MiniDB API"
    }


@app.get("/health")
def health():
    return {
        "status": "ok",
        "project": "MiniDB",
        "data_dir": str(settings.data_dir),
    }
