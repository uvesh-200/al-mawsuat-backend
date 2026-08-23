from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import get_current_user
from app.core.db import get_db
from app.models.tables import Book, ProcessingJob, User
from app.core.redis import get_redis
from app.core.storage import storage

router = APIRouter(prefix="/admin", tags=["admin"])


class AdminStatsOut(BaseModel):
    total_books: int
    total_chunks: int
    queries_today: int
    queries_this_week: int
    cache_hit_rate: float
    avg_response_ms: int
    active_jobs: int
    storage_used_mb: float
    recent_books: list[dict]


@router.get("/ping")
async def ping() -> dict:
    return {"status": "ok"}


@router.get("/stats")
async def get_stats(
    user: Annotated[User, Depends(get_current_user)] = None,
    session: Annotated[AsyncSession, Depends(get_db)] = None,
) -> AdminStatsOut:
    tenant_id = settings.DEFAULT_TENANT_ID

    total_books_result = await session.execute(
        select(func.count(Book.id)).where(Book.tenant_id == tenant_id)
    )
    total_books = total_books_result.scalar() or 0

    total_chunks_result = await session.execute(
        select(func.coalesce(func.sum(Book.total_chunks), 0)).where(Book.tenant_id == tenant_id)
    )
    total_chunks = total_chunks_result.scalar() or 0

    active_jobs_result = await session.execute(
        select(func.count(ProcessingJob.id)).where(
            ProcessingJob.tenant_id == tenant_id,
            ProcessingJob.status.notin_(["completed", "failed"]),
        )
    )
    active_jobs = active_jobs_result.scalar() or 0

    recent_result = await session.execute(
        select(Book)
        .where(Book.tenant_id == tenant_id)
        .order_by(Book.created_at.desc())
        .limit(5)
    )
    recent_books_raw = recent_result.scalars().all()

    recent_books = [
        {
            "id": str(b.id),
            "title": b.title,
            "author": b.author,
            "language": b.language,
            "book_type": b.book_type,
            "status": b.status,
            "total_chunks": b.total_chunks,
            "total_pages": b.total_pages,
            "created_at": b.created_at.isoformat() if b.created_at else "",
        }
        for b in recent_books_raw
    ]

    # Redis stats
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    year_week = datetime.now(timezone.utc).strftime("%Y-%W")
    r = await get_redis()

    queries_today_raw = await r.get(f"stats:{tenant_id}:queries:{today}")
    queries_today = int(queries_today_raw) if queries_today_raw else 0

    queries_week_raw = await r.get(f"stats:{tenant_id}:queries_week:{year_week}")
    queries_this_week = int(queries_week_raw) if queries_week_raw else 0

    cache_hits_raw = await r.get(f"stats:{tenant_id}:cache_hits:{today}")
    cache_misses_raw = await r.get(f"stats:{tenant_id}:cache_misses:{today}")
    cache_hits = int(cache_hits_raw) if cache_hits_raw else 0
    cache_misses = int(cache_misses_raw) if cache_misses_raw else 0
    total_requests = cache_hits + cache_misses
    cache_hit_rate = round(cache_hits / total_requests, 4) if total_requests > 0 else 0.0

    rt_sum_raw = await r.get(f"stats:{tenant_id}:rt_sum:{today}")
    rt_count_raw = await r.get(f"stats:{tenant_id}:rt_count:{today}")
    rt_sum = int(rt_sum_raw) if rt_sum_raw else 0
    rt_count = int(rt_count_raw) if rt_count_raw else 0
    avg_response_ms = rt_sum // rt_count if rt_count > 0 else 0

    # Storage estimate from MinIO
    storage_used_mb = 0.0
    try:
        objects = await asyncio.to_thread(
            storage._client.list_objects,
            settings.MINIO_BUCKET_BOOKS,
            recursive=True,
        )
        total_bytes = sum(obj.size for obj in objects)
        storage_used_mb = round(total_bytes / (1024 * 1024), 2)
    except Exception:
        logger = logging.getLogger(__name__)
        logger.exception("Failed to get storage usage from MinIO")
        storage_used_mb = round(total_chunks * 0.05, 2)

    return AdminStatsOut(
        total_books=total_books,
        total_chunks=total_chunks,
        queries_today=queries_today,
        queries_this_week=queries_this_week,
        cache_hit_rate=cache_hit_rate,
        avg_response_ms=avg_response_ms,
        active_jobs=active_jobs,
        storage_used_mb=storage_used_mb,
        recent_books=recent_books,
    )
