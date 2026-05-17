from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from celery import Celery
from sqlalchemy import select, update

from app.config import settings
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


async def _update_job(
    book_id: str, *, status: str, progress_pct: int, current_step: str | None = None, error_msg: str | None = None,
) -> None:
    async with AsyncSessionLocal() as session:
        values: dict = {
            "status": status,
            "progress_pct": progress_pct,
        }
        if current_step is not None:
            values["current_step"] = current_step
        if error_msg is not None:
            values["error_msg"] = error_msg
        if status == "extracting":
            values["started_at"] = datetime.now(timezone.utc)
        if status in ("completed", "failed"):
            values["finished_at"] = datetime.now(timezone.utc)
        stmt = (
            update(ProcessingJob)
            .where(ProcessingJob.book_id == book_id)
            .values(**values)
        )
        await session.execute(stmt)
        await session.commit()


async def _get_book(book_id: str) -> Book | None:
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Book).where(Book.id == book_id))
        return result.scalar_one_or_none()


async def _process_book_async(book_id: str, minio_path: str, tenant_id: str) -> None:
    # Step 1: extracting
    await _update_job(book_id, status="extracting", progress_pct=10, current_step="extracting")
    pdf_bytes = await storage.get_file(settings.MINIO_BUCKET_BOOKS, minio_path)
    pages = await asyncio.to_thread(extract, pdf_bytes)

    # Step 2: chunking
    await _update_job(book_id, status="chunking", progress_pct=30, current_step="chunking")
    chunks = await asyncio.to_thread(chunk_text, pages, tenant_id)

    # Enrich chunks with book metadata
    book = await _get_book(book_id)
    if book is None:
        raise ValueError(f"Book {book_id} not found")
    total_pages = len(pages)
    async with AsyncSessionLocal() as session:
        stmt = update(Book).where(Book.id == book_id).values(total_pages=total_pages)
        await session.execute(stmt)
        await session.commit()

    for chunk in chunks:
        chunk["book_id"] = str(book.id)
        chunk["book_name"] = book.title
        chunk["author"] = book.author or ""
        chunk["language"] = book.language
        chunk["book_type"] = book.book_type or ""
        chunk["minio_path"] = minio_path

    # Step 3: embedding
    await _update_job(book_id, status="embedding", progress_pct=60, current_step="embedding")
    vectors = await embed_chunks(chunks)

    # Step 4: indexing
    await _update_job(book_id, status="indexing", progress_pct=85, current_step="indexing")
    await index_to_qdrant(chunks, vectors)
    await index_to_meilisearch(chunks)

    # Step 5: update book status
    async with AsyncSessionLocal() as session:
        await update_book_status(book_id, len(chunks), session)

    # Step 6: completed
    await _update_job(book_id, status="completed", progress_pct=100, current_step="completed")


@celery_app.task(bind=True, max_retries=0, acks_late=True)
def process_book(
    self,
    book_id: str,
    minio_path: str,
    tenant_id: str = settings.DEFAULT_TENANT_ID,
) -> None:
    try:
        asyncio.run(_process_book_async(book_id, minio_path, tenant_id))
    except Exception as exc:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(
                _update_job(
                    book_id,
                    status="failed",
                    progress_pct=0,
                    current_step="failed",
                    error_msg=str(exc),
                )
            )
        finally:
            loop.close()
        raise
