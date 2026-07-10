from fastapi import FastAPI

from app.api.routers import banks, jobs, models, webhooks

app = FastAPI(title="autocreate", version="0.2.0")

app.include_router(models.router)
app.include_router(banks.router)
app.include_router(jobs.router)
app.include_router(webhooks.router)


@app.get("/health")
def health():
    return {"status": "ok"}
