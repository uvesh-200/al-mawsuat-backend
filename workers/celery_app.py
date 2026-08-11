from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import redis.asyncio as aioredis
from celery import Celery
from sqlalchemy import delete as sa_delete, select, update

from app.config import settings
from app.models.db import AsyncSessionLocal
from app.models.tables import Book, ProcessingJob
from workers.processor import process_book_async

logger = logging.getLogger(__name__)

celery_app = Celery(
    "mawsuat",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
)

celery_app.conf.beat_schedule = {
    "dispatch-pending": {
        "task": "workers.celery_app.dispatch_pending",
        "schedule": 60.0,
    },
    "reap-stale-jobs": {
        "task": "workers.celery_app.reap_stale_jobs",
        "schedule": 300.0,
    },
}


async def _run_with_cleanup(book_id: str, minio_path: str, tenant_id: str, task_id: str) -> None:
    try:
        async with AsyncSessionLocal() as session:
            stmt = (
                update(ProcessingJob)
                .where(ProcessingJob.book_id == book_id)
                .values(
                    task_id=task_id,
                    status="extracting",
                    started_at=datetime.now(timezone.utc),
                )
            )
            await session.execute(stmt)
            await session.commit()
        await process_book_async(book_id, minio_path, tenant_id)
    except Exception as exc:
        try:
            await _mark_failed(book_id, exc)
        except Exception as inner:
            logger.error("Failed to mark job %s as failed: %s", book_id, inner)
        raise
    finally:
        # asyncio.run() creates a fresh event loop per task; drop the cached
        # redis client so its pool is never reused across closed loops.
        from app.core.redis import close_redis
        try:
            await close_redis()
        except Exception as inner:
            logger.warning("Failed to close redis client: %s", inner)


async def _mark_failed(book_id: str, exc: Exception) -> None:
    from app.core.events import publish_book_update
    max_attempts = 3
    for attempt in range(max_attempts):
        try:
            async with AsyncSessionLocal() as session:
                stmt = (
                    update(ProcessingJob)
                    .where(ProcessingJob.book_id == book_id)
                    .values(
                        status="failed", progress_pct=0, current_step="failed",
                        error_msg=f"{type(exc).__name__}: {exc}",
                        finished_at=datetime.now(timezone.utc),
                    )
                )
                await session.execute(stmt)
                # The book stays "pending" on failure otherwise (only
                # update_book_status flips it to "ready"), so the UI keeps
                # showing "Processing…" forever. Mirror the failure to the
                # book row so the UI can display the real state.
                await session.execute(
                    update(Book)
                    .where(Book.id == book_id)
                    .values(status="failed")
                )
                await session.commit()
            break
        except Exception as inner:
            if attempt < max_attempts - 1:
                await asyncio.sleep(0.5 * (2 ** attempt))
            else:
                logger.error("Failed to mark job %s as failed: %s", book_id, inner)
    try:
        await publish_book_update({"book_id": book_id, "status": "failed"})
    except Exception:
        pass


@celery_app.task(
    bind=True, max_retries=0, acks_late=True,
    time_limit=43200, soft_time_limit=42000,
)
def process_book(
    self,
    book_id: str,
    minio_path: str,
    tenant_id: str = settings.DEFAULT_TENANT_ID,
) -> None:
    asyncio.run(_run_with_cleanup(book_id, minio_path, tenant_id, self.request.id or ""))


@celery_app.task
def dispatch_pending() -> None:
    async def _dispatch() -> None:
        r = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
        try:
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    select(ProcessingJob).where(
                        ProcessingJob.status == "queued",
                    )
                )
                jobs = result.scalars().all()
                for job in jobs:
                    lock_key = f"dispatch:lock:{job.book_id}"
                    locked = await r.setnx(lock_key, "1")
                    if not locked:
                        continue
                    await r.expire(lock_key, 120)
                    try:
                        if job.task_id:
                            key = f"worker:heartbeat:{job.book_id}"
                            alive = await r.exists(key)
                            if alive:
                                continue
                        book_result = await session.execute(
                            select(Book).where(Book.id == job.book_id)
                        )
                        book = book_result.scalar_one_or_none()
                        if book is None:
                            await session.execute(
                                sa_delete(ProcessingJob).where(ProcessingJob.id == job.id)
                            )
                            continue
                        await session.execute(
                            update(ProcessingJob)
                            .where(ProcessingJob.id == job.id)
                            .values(status="extracting", started_at=datetime.now(timezone.utc))
                        )
                        await session.commit()
                        process_book.delay(
                            book_id=str(job.book_id),
                            minio_path=book.minio_path or "",
                            tenant_id=job.tenant_id,
                        )
                    finally:
                        await r.delete(lock_key)
                await session.commit()
        finally:
            try:
                await r.aclose()
            except RuntimeError:
                pass
    asyncio.run(_dispatch())


@celery_app.task
def reap_stale_jobs() -> None:
    async def _reap() -> None:
        r = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
        try:
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    select(ProcessingJob).where(
                        ProcessingJob.status.in_(["extracting", "chunking", "embedding", "indexing"]),
                    )
                )
                candidates = result.scalars().all()
                stale = []
                for job in candidates:
                    key = f"worker:heartbeat:{job.book_id}"
                    alive = await r.exists(key)
                    if not alive:
                        stale.append(job)
                if not stale:
                    return
                for job in stale:
                    if job.task_id:
                        celery_app.control.revoke(job.task_id, terminate=True)
                    book_result = await session.execute(
                        select(Book).where(Book.id == job.book_id)
                    )
                    book = book_result.scalar_one_or_none()
                    if book is None:
                        await session.execute(
                            sa_delete(ProcessingJob).where(ProcessingJob.id == job.id)
                        )
                        continue
                    await session.execute(
                        update(ProcessingJob)
                        .where(ProcessingJob.id == job.id)
                        .values(status="queued", progress_pct=0, current_step="re-queued",
                                checkpoint=None, error_msg=None, retry_count=0)
                    )
                    await session.commit()
                    process_book.delay(
                        book_id=str(job.book_id),
                        minio_path=book.minio_path or "",
                        tenant_id=job.tenant_id,
                    )
        finally:
            try:
                await r.aclose()
            except RuntimeError:
                pass
    asyncio.run(_reap())
