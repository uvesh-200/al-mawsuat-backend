from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.models.db import get_db
from app.models.tables import Book, ProcessingJob, User

router = APIRouter(prefix="/admin", tags=["admin"])


class AdminStatsOut(BaseModel):
    total_books: int
    total_chunks: int
    queries_today: int = 0
    cache_hit_rate: float = 0.0
    avg_response_ms: int = 0
    active_jobs: int
    storage_used_mb: float = 0.0
    recent_books: list[dict]


@router.get("/ping")
async def ping() -> dict:
    return {"status": "ok"}


@router.get("/stats")
async def get_stats(
    user: Annotated[User, Depends(get_current_user)] = None,
    session: Annotated[AsyncSession, Depends(get_db)] = None,
) -> AdminStatsOut:
    tenant_id = "al-mawsuat-deobandiyyah"

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

    return AdminStatsOut(
        total_books=total_books,
        total_chunks=total_chunks,
        active_jobs=active_jobs,
        recent_books=recent_books,
    )
