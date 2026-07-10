from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from app.api.routers import auth, banks, jobs, models, webhooks

app = FastAPI(title="autocreate", version="0.5.0")

app.include_router(auth.router)
app.include_router(models.router)
app.include_router(banks.router)
app.include_router(jobs.router)
app.include_router(webhooks.router)

_STATIC = Path(__file__).parent / "static"


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(_STATIC / "index.html")


@app.get("/login", include_in_schema=False)
def login_page():
    return FileResponse(_STATIC / "login.html")


@app.get("/health")
def health():
    return {"status": "ok"}
