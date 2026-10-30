"""Read-only API of the simulated target system. Run: python -m simulated_target_service (port 8100)."""
from fastapi import FastAPI

from simulated_target_service import database

app = FastAPI(title="Simulated Simplicity target system", docs_url=None, redoc_url=None)


@app.on_event("startup")
def _startup() -> None:
    database.bootstrap()


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "system": database.read_schema()["system"]}


@app.get("/schema")
def schema() -> dict:
    """Tables, columns, types, required flags, data-dictionary descriptions and code lists. No row data."""
    return database.read_schema()
