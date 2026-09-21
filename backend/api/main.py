from fastapi import FastAPI

app = FastAPI(
    title="MiniDB API",
    version="0.1.0"
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
        "project": "MiniDB"
    }