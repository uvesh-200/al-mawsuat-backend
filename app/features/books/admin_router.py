from __future__ import annotations

import asyncio
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import delete as sa_delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import get_current_user
from app.core.db import get_db
from app.models.tables import Book, ProcessingJob, User
from app.workers.celery_app import process_book

router = APIRouter(prefix="/admin/books", tags=["books_admin"])


class BookUpdate(BaseModel):
    title: str
    author: str | None = None
    language: str
    book_type: str | None = None


async def _delete_from_qdrant(book_id: str) -> None:
    try:
        from qdrant_client import AsyncQdrantClient
        from qdrant_client.models import FieldCondition, Filter, FilterSelector, MatchValue
        client = AsyncQdrantClient(host=settings.QDRANT_HOST, port=settings.QDRANT_PORT, timeout=10)
        try:
            collections = await client.get_collections()
            if not any(c.name == "documents" for c in collections.collections):
                return
            await client.delete(
                collection_name="documents",
                points_selector=FilterSelector(
                    filter=Filter(must=[
                        FieldCondition(key="book_id", match=MatchValue(value=book_id)),
                        FieldCondition(key="tenant_id", match=MatchValue(value=settings.DEFAULT_TENANT_ID)),
                    ])
                ),
            )
        finally:
            await client.close()
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("Failed to delete book %s from Qdrant: %s", book_id, e)


async def _delete_from_meilisearch(book_id: str) -> None:
    try:
        import meilisearch
        client = meilisearch.Client(settings.MEILISEARCH_URL, settings.MEILISEARCH_KEY)
        index = client.index("documents")
        # Paginate until dry: a single 1000-hit page silently left stale docs
        # behind for books with more chunks than the limit.
        while True:
            resp = index.search("", opt_params={"filter": [f"book_id={book_id}"], "limit": 1000})
            ids = [h["id"] for h in resp.get("hits", [])]
            if not ids:
                break
            index.delete_documents(ids)
    except meilisearch.errors.MeilisearchApiError:
        pass
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("Failed to delete book %s from Meilisearch: %s", book_id, e)


async def _update_qdrant_payload(book_id: str, data: BookUpdate) -> None:
    try:
        from qdrant_client import AsyncQdrantClient
        from qdrant_client.models import FieldCondition, Filter, MatchValue
        client = AsyncQdrantClient(host=settings.QDRANT_HOST, port=settings.QDRANT_PORT, timeout=10)
        await client.set_payload(
            collection_name="documents",
            payload={
                "book_name": data.title, "author": data.author or None,
                "language": data.language, "book_type": data.book_type or None,
            },
            points_selector=Filter(must=[
                FieldCondition(key="book_id", match=MatchValue(value=book_id)),
                FieldCondition(key="tenant_id", match=MatchValue(value=settings.DEFAULT_TENANT_ID)),
            ])
        )
        await client.close()
    except Exception as e:
        import logging
        logging.warning(f"Failed to update Qdrant points payload for book {book_id}: {e}")


async def _update_meilisearch_docs(book_id: str, data: BookUpdate) -> None:
    try:
        import meilisearch
        client = meilisearch.Client(settings.MEILISEARCH_URL, settings.MEILISEARCH_KEY)
        resp = client.index("documents").search("", opt_params={"filter": [f"book_id={book_id}"], "limit": 1000})
        hits = resp.get("hits", [])
        if hits:
            docs = []
            for h in hits:
                doc = dict(h)
                doc["book_name"] = data.title
                doc["author"] = data.author or None
                doc["language"] = data.language
                doc["book_type"] = data.book_type or None
                docs.append(doc)
            client.index("documents").add_documents(docs)
    except Exception as e:
        import logging
        logging.warning(f"Failed to update Meilisearch documents for book {book_id}: {e}")


@router.get("/{book_id}/chunks")
async def list_book_chunks(
    book_id: str,
    user: Annotated[User, Depends(get_current_user)] = None,
) -> list[dict]:
    import meilisearch
    client = meilisearch.Client(settings.MEILISEARCH_URL, settings.MEILISEARCH_KEY)
    try:
        resp = client.index("documents").search("", opt_params={"filter": [f"book_id={book_id}"], "limit": 200})
        hits = resp.get("hits", [])
        return [{"id": h.get("id"), "page": h.get("page"), "text_preview": (h.get("text") or "")[:200], "score": h.get("_rankingScore", 0)} for h in hits]
    except Exception:
        return []


@router.put("/{book_id}")
async def update_book(
    book_id: str,
    data: BookUpdate,
    user: Annotated[User, Depends(get_current_user)] = None,
    session: Annotated[AsyncSession, Depends(get_db)] = None,
):
    result = await session.execute(select(Book).where(Book.id == uuid.UUID(book_id)))
    book = result.scalar_one_or_none()
    if book is None or book.tenant_id != settings.DEFAULT_TENANT_ID:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Book not found")

    book.title = data.title
    book.author = data.author or None
    book.language = data.language
    book.book_type = data.book_type or None
    await session.commit()
    await session.refresh(book)

    if book.status == "ready":
        await _update_qdrant_payload(book_id, data)
        await _update_meilisearch_docs(book_id, data)

    from app.features.books.router import BookOut
    return BookOut(
        id=str(book.id), title=book.title, author=book.author,
        language=book.language, book_type=book.book_type, status=book.status,
        total_chunks=book.total_chunks, total_pages=book.total_pages,
        minio_path=book.minio_path,
        created_at=book.created_at.isoformat() if book.created_at else "",
    )


@router.delete("/{book_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def delete_book(
    book_id: str,
    user: Annotated[User, Depends(get_current_user)] = None,
    session: Annotated[AsyncSession, Depends(get_db)] = None,
) -> None:
    """Soft-delete a book.

    Nothing is destroyed: the row, its processing jobs, the MinIO PDF and all
    derived data (Qdrant vectors, Meilisearch docs) are kept. The book is only
    flagged (books.deleted_at) and every read/retrieval path excludes it.
    Use POST /{book_id}/restore to bring it back instantly.
    """
    import datetime

    result = await session.execute(select(Book).where(Book.id == uuid.UUID(book_id)))
    book = result.scalar_one_or_none()
    if book is None or book.tenant_id != settings.DEFAULT_TENANT_ID:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Book not found")
    if book.deleted_at is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="Book already deleted")

    job_result = await session.execute(
        select(ProcessingJob).where(ProcessingJob.book_id == uuid.UUID(book_id))
    )
    job = job_result.scalar_one_or_none()
    if job is not None and job.task_id:
        try:
            from app.workers.celery_app import celery_app
            celery_app.control.revoke(job.task_id, terminate=True)
        except Exception:
            pass

    book.deleted_at = datetime.datetime.now(datetime.timezone.utc)
    await session.commit()

    from app.features.qa.cache import invalidate_tenant_cache
    await invalidate_tenant_cache(book.tenant_id)


@router.post("/{book_id}/restore", status_code=status.HTTP_202_ACCEPTED)
async def restore_book(
    book_id: str,
    user: Annotated[User, Depends(get_current_user)] = None,
    session: Annotated[AsyncSession, Depends(get_db)] = None,
) -> dict:
    """Undo a soft delete; vectors and search docs were never removed."""
    result = await session.execute(select(Book).where(Book.id == uuid.UUID(book_id)))
    book = result.scalar_one_or_none()
    if book is None or book.tenant_id != settings.DEFAULT_TENANT_ID:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Book not found")
    if book.deleted_at is None:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="Book is not deleted")

    book.deleted_at = None
    await session.commit()

    from app.features.qa.cache import invalidate_tenant_cache
    await invalidate_tenant_cache(book.tenant_id)
    return {"book_id": book_id, "restored": True}


@router.post("/{book_id}/reprocess", status_code=status.HTTP_202_ACCEPTED)
async def reprocess_book(
    book_id: str,
    user: Annotated[User, Depends(get_current_user)] = None,
    session: Annotated[AsyncSession, Depends(get_db)] = None,
) -> dict:
    result = await session.execute(select(Book).where(Book.id == uuid.UUID(book_id)))
    book = result.scalar_one_or_none()
    if book is None or book.tenant_id != settings.DEFAULT_TENANT_ID:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Book not found")
    if book.deleted_at is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="Book is deleted; restore it first")

    old_job_result = await session.execute(
        select(ProcessingJob).where(ProcessingJob.book_id == uuid.UUID(book_id))
    )
    old_job = old_job_result.scalar_one_or_none()
    if old_job is not None and old_job.task_id:
        try:
            from app.workers.celery_app import celery_app
            celery_app.control.revoke(old_job.task_id, terminate=True)
        except Exception:
            pass
        await session.execute(sa_delete(ProcessingJob).where(ProcessingJob.book_id == uuid.UUID(book_id)))

    await asyncio.gather(
        _delete_from_qdrant(book_id), _delete_from_meilisearch(book_id)
    )

    book.status = "pending"
    book.total_chunks = 0
    book.total_pages = None

    job_id = str(uuid.uuid4())
    job = ProcessingJob(id=uuid.UUID(job_id), book_id=uuid.UUID(book_id), tenant_id=book.tenant_id)
    session.add(job)
    await session.commit()

    process_book.delay(book_id=str(book.id), minio_path=book.minio_path, tenant_id=book.tenant_id)
    return {"job_id": job_id, "book_id": book_id}
