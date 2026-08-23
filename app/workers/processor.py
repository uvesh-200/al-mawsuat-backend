from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import redis.asyncio as aioredis
from sqlalchemy import select, update

from app.core.config import settings
from app.core.embedder import embed_texts
from app.core.events import publish_book_update, publish_job_update
from app.core.db import AsyncSessionLocal
from app.models.tables import Book, ProcessingJob
from app.pipeline.chunker import chunk as chunk_text
from app.pipeline.extractor import extract
from app.pipeline.indexer import index_to_meilisearch, index_to_qdrant, update_book_status
from app.pipeline.page_number_validator import IngestionPageNumberError, validate_ingestion_page_numbers
from app.core.storage import storage

logger = logging.getLogger(__name__)

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
    now = datetime.now(timezone.utc)
    max_attempts = 3
    for attempt in range(max_attempts):
        try:
            async with AsyncSessionLocal() as session:
                values: dict = {"status": status, "progress_pct": progress_pct}
                if status != "failed":
                    values["heartbeat_at"] = now
                if current_step is not None:
                    values["current_step"] = current_step
                if error_msg is not None:
                    values["error_msg"] = error_msg
                if checkpoint is not None:
                    values["checkpoint"] = checkpoint
                if status in ("extracting", "chunking", "embedding", "indexing"):
                    stmt_sa = select(ProcessingJob.started_at).where(ProcessingJob.book_id == book_id)
                    row = await session.execute(stmt_sa)
                    existing_started_at = row.scalar_one_or_none()
                    if existing_started_at is None:
                        values["started_at"] = now
                if status in ("completed", "failed"):
                    values["finished_at"] = now
                stmt = update(ProcessingJob).where(ProcessingJob.book_id == book_id).values(**values)
                await session.execute(stmt)
                await session.commit()
            break
        except Exception as exc:
            if attempt < max_attempts - 1:
                wait = 0.5 * (2 ** attempt)
                logger.warning("_update_job attempt %d failed, retrying in %.1fs: %s", attempt + 1, wait, exc)
                await asyncio.sleep(wait)
            else:
                logger.error("_update_job failed after %d attempts: %s", max_attempts, exc)
                raise
    await publish_job_update({
        "book_id": book_id, "status": status, "progress_pct": progress_pct,
        "current_step": current_step, "error_msg": error_msg,
    })


async def _heartbeat_loop(book_id: str, stop_event: asyncio.Event) -> None:
    r = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    key = f"worker:heartbeat:{book_id}"
    try:
        while not stop_event.is_set():
            try:
                await r.setex(key, 60, datetime.now(timezone.utc).isoformat())
            except Exception:
                pass
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=HEARTBEAT_INTERVAL)
            except asyncio.TimeoutError:
                pass
    finally:
        try:
            await r.aclose()
        except RuntimeError:
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


async def process_book_async(book_id: str, minio_path: str, tenant_id: str) -> None:
    job = await _read_job(book_id)
    if job is None:
        logger.warning("No processing_job for book %s (was it deleted?)", book_id)
        return

    book = await _get_book(book_id)
    if book is None:
        logger.warning("Book %s was deleted before processing started, skipping", book_id)
        return

    start_phase = _phase_index("queued")
    start_page = 0
    if job.checkpoint:
        start_phase = _phase_index(job.checkpoint.get("phase", "queued"))
        start_page = job.checkpoint.get("page", 0)

    logger.info(
        "[TRACE] phase=job_start book_id=%s tenant=%s minio_path=%s resume_phase=%s",
        book_id, tenant_id, minio_path, PHASE_ORDER[start_phase],
    )

    stop_heartbeat = asyncio.Event()
    heartbeat_task = asyncio.ensure_future(_heartbeat_loop(book_id, stop_heartbeat))

    pages: list = []
    chunks: list = []
    vectors: list = []

    try:
        if start_phase <= _phase_index("extracting"):
            pdf_bytes = await storage.get_file(settings.MINIO_BUCKET_BOOKS, minio_path)
            logger.info("[TRACE] phase=extract book_id=%s pdf_bytes=%d", book_id, len(pdf_bytes))
            progress = {"current": 0, "total": 0}

            def _on_page_done(current: int, total: int) -> None:
                progress["current"] = current
                progress["total"] = total

            extract_task = asyncio.ensure_future(extract(pdf_bytes, _on_page_done))
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
                        current_step=step,
                        checkpoint=chk if c >= last_checkpoint_page + CHECKPOINT_INTERVAL else None,
                    )
                    if c >= last_checkpoint_page + CHECKPOINT_INTERVAL:
                        last_checkpoint_page = c
                await asyncio.sleep(2)

            pages = extract_task.result()
            total_pages = len(pages)
            total_words = sum(len(p.get("words", [])) for p in pages)
            footers = sum(1 for p in pages if p.get("footer_extracted"))
            logger.info(
                "[TRACE] phase=extract_done book_id=%s pages=%d words=%d avg_words_per_page=%.1f "
                "footer_extracted=%d/%d",
                book_id, total_pages, total_words,
                total_words / total_pages if total_pages else 0,
                footers, total_pages,
            )

            # Validate footer-extracted page numbers before ingesting
            try:
                pn_report = validate_ingestion_page_numbers(pages)
                logger.info(
                    "Page-number validation passed: %d/%d pages had footer extraction, "
                    "modal_offset=%s",
                    pn_report.footer_extracted, pn_report.total_pages, pn_report.offset,
                )
            except IngestionPageNumberError as pn_err:
                logger.warning(
                    "Page-number validation warning for book %s: %s — "
                    "continuing with physical page indices as fallback.",
                    book_id, pn_err,
                )

            async with AsyncSessionLocal() as session:
                await session.execute(update(Book).where(Book.id == book_id).values(total_pages=total_pages))
                await session.commit()

            await _update_job(
                book_id, status="chunking", progress_pct=30, current_step="chunking",
                checkpoint={"phase": "chunking"},
            )

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

            tokens = [c.get("token_count", 0) for c in chunks]
            logger.info(
                "[TRACE] phase=chunk_done book_id=%s chunks=%d tokens_total=%d avg_tokens=%.1f max_tokens=%d",
                book_id, len(chunks), sum(tokens),
                sum(tokens) / len(tokens) if tokens else 0,
                max(tokens) if tokens else 0,
            )

            await _update_job(
                book_id, status="embedding", progress_pct=60, current_step="embedding",
                checkpoint={"phase": "embedding"},
            )

        if start_phase <= _phase_index("embedding"):
            texts = [c["text"] for c in chunks]
            vectors = await embed_texts(texts)
            logger.info(
                "[TRACE] phase=embed_done book_id=%s vectors=%d dim=%d",
                book_id, len(vectors), len(vectors[0]) if vectors else 0,
            )
            await _update_job(
                book_id, status="indexing", progress_pct=85, current_step="indexing",
                checkpoint={"phase": "indexing"},
            )

        if start_phase <= _phase_index("indexing"):
            await index_to_qdrant(chunks, vectors)
            await index_to_meilisearch(chunks)
            async with AsyncSessionLocal() as session:
                await update_book_status(book_id, len(chunks), session)
            logger.info(
                "[TRACE] phase=index_done book_id=%s chunks_indexed=%d",
                book_id, len(chunks),
            )
            await _update_job(
                book_id, status="completed", progress_pct=100, current_step="completed",
                checkpoint={"phase": "completed"},
            )
            await publish_book_update({"book_id": book_id, "status": "ready"})
    finally:
        stop_heartbeat.set()
        try:
            await heartbeat_task
        except RuntimeError:
            pass
