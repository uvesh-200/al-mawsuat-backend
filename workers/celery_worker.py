from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from celery import Celery
from sqlalchemy import select, update

from app.config import settings
from app.core.events import publish_job_update, publish_book_update
from app.models.db import AsyncSessionLocal
from app.models.tables import Book, ProcessingJob
from app.pipeline.extractor import extract
from app.pipeline.chunker import chunk as chunk_text
from app.pipeline.embedder_batch import embed_chunks
from app.pipeline.indexer import index_to_qdrant, index_to_meilisearch, update_book_status
from app.storage.minio_client import storage

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
    "reap-stale-jobs": {
        "task": "workers.celery_worker.reap_stale_jobs",
        "schedule": 300.0,
    },
}

CHECKPOINT_INTERVAL = 50
HEARTBEAT_INTERVAL = 30

PHASE_ORDER = ["queued", "extracting", "chunking", "embedding", "indexing", "completed"]


async def _read_job(book_id: str) -> ProcessingJob | None:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(ProcessingJob).where(ProcessingJob.book_id == book_id)
        )
        return result.scalar_one_or_none()


async def _update_job(
    book_id: str, *, status: str, progress_pct: int, current_step: str | None = None,
    error_msg: str | None = None, checkpoint: dict | None = None,
) -> None:
    async with AsyncSessionLocal() as session:
        values: dict = {
            "status": status,
            "progress_pct": progress_pct,
            "heartbeat_at": datetime.now(timezone.utc),
        }
        if current_step is not None:
            values["current_step"] = current_step
        if error_msg is not None:
            values["error_msg"] = error_msg
        if checkpoint is not None:
            values["checkpoint"] = checkpoint
        if status in ("completed", "failed"):
            values["finished_at"] = datetime.now(timezone.utc)
        stmt = (
            update(ProcessingJob)
            .where(ProcessingJob.book_id == book_id)
            .values(**values)
        )
        await session.execute(stmt)
        await session.commit()
    await publish_job_update({
        "book_id": book_id,
        "status": status,
        "progress_pct": progress_pct,
        "current_step": current_step,
        "error_msg": error_msg,
    })


async def _heartbeat_loop(book_id: str, stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        try:
            async with AsyncSessionLocal() as session:
                await session.execute(
                    update(ProcessingJob)
                    .where(ProcessingJob.book_id == book_id)
                    .values(heartbeat_at=datetime.now(timezone.utc))
                )
                await session.commit()
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=HEARTBEAT_INTERVAL)
        except asyncio.TimeoutError:
            pass


async def _get_book(book_id: str) -> Book | None:
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Book).where(Book.id == book_id))
        return result.scalar_one_or_none()


def _phase_index(status: str) -> int:
    try:
        return PHASE_ORDER.index(status)
    except ValueError:
        return 0


async def _process_book_async(book_id: str, minio_path: str, tenant_id: str) -> None:
    job = await _read_job(book_id)
    if job is None:
        raise ValueError(f"No processing_job for book {book_id}")

    start_phase = _phase_index("queued")
    if job.checkpoint:
        start_phase = _phase_index(job.checkpoint.get("phase", "queued"))
    start_page = 0
    if job.checkpoint:
        start_page = job.checkpoint.get("page", 0)

    stop_heartbeat = asyncio.Event()
    heartbeat_task = asyncio.ensure_future(_heartbeat_loop(book_id, stop_heartbeat))

    try:
        # --- extracting ---
        if start_phase <= _phase_index("extracting"):
            pdf_bytes = await storage.get_file(settings.MINIO_BUCKET_BOOKS, minio_path)
            progress = {"current": 0, "total": 0}

            def _on_page_done(current: int, total: int) -> None:
                progress["current"] = current
                progress["total"] = total

            extract_coro = asyncio.to_thread(extract, pdf_bytes, _on_page_done)
            extract_task = asyncio.ensure_future(extract_coro)

            last_checkpoint_page = start_page

            while not extract_task.done():
                if progress["total"] > 0:
                    c = progress["current"]
                    t = progress["total"]
                    pct = 10 + int((c / t) * 15)
                    step = f"extracting ({c}/{t})"
                    chk = {"phase": "extracting", "page": c}
                    await _update_job(
                        book_id, status="extracting", progress_pct=pct,
                        current_step=step, checkpoint=chk if c >= last_checkpoint_page + CHECKPOINT_INTERVAL else None,
                    )
                    if c >= last_checkpoint_page + CHECKPOINT_INTERVAL:
                        last_checkpoint_page = c
                await asyncio.sleep(2)

            pages = extract_task.result()
            total_pages = len(pages)
            async with AsyncSessionLocal() as session:
                await session.execute(
                    update(Book).where(Book.id == book_id).values(total_pages=total_pages)
                )
                await session.commit()

            await _update_job(
                book_id, status="chunking", progress_pct=30, current_step="chunking",
                checkpoint={"phase": "chunking"},
            )

        # --- chunking ---
        if start_phase <= _phase_index("chunking"):
            chunks = await asyncio.to_thread(chunk_text, pages, tenant_id)

            book = await _get_book(book_id)
            if book is None:
                raise ValueError(f"Book {book_id} not found")

            for chunk in chunks:
                chunk["book_id"] = str(book.id)
                chunk["book_name"] = book.title
                chunk["author"] = book.author or ""
                chunk["language"] = book.language
                chunk["book_type"] = book.book_type or ""
                chunk["minio_path"] = minio_path

            await _update_job(
                book_id, status="embedding", progress_pct=60, current_step="embedding",
                checkpoint={"phase": "embedding"},
            )

        # --- embedding ---
        if start_phase <= _phase_index("embedding"):
            vectors = await embed_chunks(chunks)

            await _update_job(
                book_id, status="indexing", progress_pct=85, current_step="indexing",
                checkpoint={"phase": "indexing"},
            )

        # --- indexing ---
        if start_phase <= _phase_index("indexing"):
            await index_to_qdrant(chunks, vectors)
            await index_to_meilisearch(chunks)

            async with AsyncSessionLocal() as session:
                await update_book_status(book_id, len(chunks), session)

            await _update_job(
                book_id, status="completed", progress_pct=100, current_step="completed",
                checkpoint={"phase": "completed"},
            )
            await publish_book_update({"book_id": book_id, "status": "ready"})

    finally:
        stop_heartbeat.set()
        await heartbeat_task


async def _run_with_cleanup(book_id: str, minio_path: str, tenant_id: str, task_id: str) -> None:
    from app.models.db import engine as _engine

    try:
        # Save task_id and increment retry_count
        async with AsyncSessionLocal() as session:
            stmt = (
                update(ProcessingJob)
                .where(ProcessingJob.book_id == book_id)
                .values(task_id=task_id)
            )
            await session.execute(stmt)
            await session.commit()

        await _process_book_async(book_id, minio_path, tenant_id)
    except Exception as exc:
        await _update_job(
            book_id,
            status="failed",
            progress_pct=0,
            current_step="failed",
            error_msg=f"{type(exc).__name__}: {exc}",
        )
        await publish_book_update({"book_id": book_id, "status": "failed"})
        raise
    finally:
        await _engine.dispose()


@celery_app.task(
    bind=True, max_retries=0, acks_late=True,
    time_limit=7200, soft_time_limit=6900,
)
def process_book(
    self,
    book_id: str,
    minio_path: str,
    tenant_id: str = settings.DEFAULT_TENANT_ID,
) -> None:
    asyncio.run(_run_with_cleanup(book_id, minio_path, tenant_id, self.request.id or ""))


@celery_app.task
def reap_stale_jobs() -> None:
    async def _reap() -> None:
        from datetime import timedelta

        async with AsyncSessionLocal() as session:
            cutoff = datetime.now(timezone.utc) - timedelta(minutes=5)
            result = await session.execute(
                select(ProcessingJob).where(
                    ProcessingJob.status.in_(["extracting", "chunking", "embedding", "indexing"]),
                    ProcessingJob.heartbeat_at < cutoff,
                )
            )
            stale = result.scalars().all()
            if not stale:
                return

            for job in stale:
                if job.task_id:
                    celery_app.control.revoke(job.task_id, terminate=True)
                await session.execute(
                    update(ProcessingJob)
                    .where(ProcessingJob.id == job.id)
                    .values(status="queued", progress_pct=0, current_step="re-queued")
                )
            await session.commit()

            for job in stale:
                book_result = await session.execute(
                    select(Book).where(Book.id == job.book_id)
                )
                book = book_result.scalar_one_or_none()
                if book is None:
                    continue
                process_book.delay(
                    book_id=str(job.book_id),
                    minio_path=book.minio_path or "",
                    tenant_id=job.tenant_id,
                )

    asyncio.run(_reap())
