from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.admin import router as admin_router
from app.api.ask import router as ask_router
from app.api.auth import router as auth_router
from app.api.books import router as books_router
from app.api.jobs import router as jobs_router
from app.config import settings
from app.storage.minio_client import storage


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None, None]:
    await storage.ensure_buckets()
    yield


app = FastAPI(title="Al-Mawsu'at al-Deobandiyyah API", lifespan=lifespan)

app.include_router(auth_router)
app.include_router(admin_router)
app.include_router(ask_router)
app.include_router(books_router)
app.include_router(jobs_router)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "environment": settings.ENVIRONMENT}

