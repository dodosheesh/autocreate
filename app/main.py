from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from app.api.routers import banks, jobs, models, webhooks

app = FastAPI(title="autocreate", version="0.4.0")

app.include_router(models.router)
app.include_router(banks.router)
app.include_router(jobs.router)
app.include_router(webhooks.router)

_INDEX = Path(__file__).parent / "static" / "index.html"


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(_INDEX)


@app.get("/health")
def health():
    return {"status": "ok"}
