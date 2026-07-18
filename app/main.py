import json
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import sentry_sdk
from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration

from app.api.admin import router as admin_router
from app.api.ask import router as ask_router
from app.api.auth import router as auth_router
from app.api.books import router as books_router
from app.api.books_admin import router as books_admin_router
from app.api.events import router as events_router
from app.api.highlight import router as highlight_router
from app.api.jobs import router as jobs_router
from app.api.settings import router as settings_router
from app.api.users import router as users_router
from app.config import settings
from app.core.health import check_postgres, check_qdrant, check_redis
from app.core.logging import StructuredLoggingMiddleware
from app.storage.minio_client import storage

if settings.SENTRY_DSN:
    sentry_sdk.init(
        dsn=settings.SENTRY_DSN,
        integrations=[FastApiIntegration(), SqlalchemyIntegration()],
        traces_sample_rate=0.1,
        environment=settings.ENVIRONMENT,
    )

logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
    format='%(message)s',
)
logging.getLogger("api").setLevel(getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO))


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None, None]:
    await storage.ensure_buckets()
    yield


app = FastAPI(title="Al-Mawsu'at al-Deobandiyyah API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_origin_regex=settings.CORS_ORIGIN_REGEX,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(StructuredLoggingMiddleware)

app.include_router(auth_router)
app.include_router(admin_router)
app.include_router(ask_router)
app.include_router(books_router)
app.include_router(books_admin_router)
app.include_router(events_router)
app.include_router(highlight_router)
app.include_router(jobs_router)
app.include_router(settings_router)
app.include_router(users_router)


@app.get("/health")
async def health() -> dict:
    pg = await check_postgres()
    qd = await check_qdrant()
    rd = await check_redis()
    checks = {"postgres": pg, "qdrant": qd, "redis": rd}
    if all(v == "ok" for v in checks.values()):
        return {"status": "ok", "checks": checks, "environment": settings.ENVIRONMENT}
    return Response(
        content=json.dumps({"status": "degraded", "checks": checks, "environment": settings.ENVIRONMENT}),
        media_type="application/json",
        status_code=503,
    )
