from __future__ import annotations

import uuid
from typing import Annotated, Literal

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import delete as sa_delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.auth import get_current_user
from app.models.db import get_db
from app.models.tables import Book, ProcessingJob, User
from app.storage.minio_client import storage
from workers.celery_worker import process_book

router = APIRouter(prefix="/admin/books", tags=["books"])


class BookOut(BaseModel):
    id: str
    title: str
    author: str | None
    language: str
    book_type: str | None
    status: str
    total_chunks: int
    total_pages: int | None
    minio_path: str | None
    created_at: str


class BookListResponse(BaseModel):
    items: list[BookOut]
    total: int
    page: int
    page_size: int
    total_pages: int


class BookDetailOut(BaseModel):
    id: str
    title: str
    author: str | None
    language: str
    book_type: str | None
    status: str
    total_chunks: int
    total_pages: int | None
    minio_path: str | None
    created_at: str
    job: dict | None = None


class UploadOut(BaseModel):
    job_id: str
    book_id: str
    status: str = "queued"


class BookUpdate(BaseModel):
    title: str
    author: str | None = None
    language: str
    book_type: str | None = None



ALLOWED_SORT_FIELDS: dict[str, object] = {
    "title": Book.title,
    "author": Book.author,
    "language": Book.language,
    "book_type": Book.book_type,
    "status": Book.status,
    "total_chunks": Book.total_chunks,
    "total_pages": Book.total_pages,
    "created_at": Book.created_at,
}


@router.post("/upload", response_model=UploadOut, status_code=status.HTTP_201_CREATED)
async def upload_book(
    file: UploadFile,
    title: str = Form(...),
    author: str = Form(""),
    language: str = Form("ar"),
    book_type: str = Form(""),
    user: Annotated[User, Depends(get_current_user)] = None,
    session: Annotated[AsyncSession, Depends(get_db)] = None,
) -> UploadOut:
    # Validate PDF
    if file.content_type and file.content_type != "application/pdf":
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail="File must be a PDF")
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail="File must have a .pdf extension")

    data = await file.read()
    if len(data) < 5 or data[:4] != b"%PDF":
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail="File is not a valid PDF")

    tenant_id = settings.DEFAULT_TENANT_ID
    book_id = str(uuid.uuid4())
    job_id = str(uuid.uuid4())
    minio_path = f"books/{tenant_id}/{book_id}/original.pdf"

    # Upload to MinIO
    await storage.upload_file(
        settings.MINIO_BUCKET_BOOKS,
        minio_path,
        data,
        file.content_type or "application/pdf",
    )

    # Create Book record
    book = Book(
        id=uuid.UUID(book_id),
        tenant_id=tenant_id,
        title=title,
        author=author or None,
        language=language,
        book_type=book_type or None,
        minio_path=minio_path,
        uploaded_by=user.id if user else None,
    )
    session.add(book)

    # Create ProcessingJob record
    job = ProcessingJob(
        id=uuid.UUID(job_id),
        book_id=uuid.UUID(book_id),
        tenant_id=tenant_id,
    )
    session.add(job)
    await session.commit()

    # Trigger Celery task
    process_book.delay(book_id=str(book.id), minio_path=minio_path, tenant_id=tenant_id)

    return UploadOut(job_id=job_id, book_id=book_id)


@router.get("", response_model=BookListResponse)
async def list_books(
    q: str = Query("", description="Search query (matches title and author)"),
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(20, ge=1, le=200, description="Items per page"),
    sort_by: Literal["title", "author", "language", "book_type", "status", "total_chunks", "total_pages", "created_at"] = Query(
        "created_at", description="Field to sort by"
    ),
    sort_order: Literal["asc", "desc"] = Query("desc", description="Sort direction"),
    user: Annotated[User, Depends(get_current_user)] = None,
    session: Annotated[AsyncSession, Depends(get_db)] = None,
) -> BookListResponse:
    tenant_filter = Book.tenant_id == settings.DEFAULT_TENANT_ID
    base = select(Book).where(tenant_filter)
    count_base = select(func.count(Book.id)).where(tenant_filter)

    if q:
        pattern = f"%{q}%"
        base = base.where(Book.title.ilike(pattern) | Book.author.ilike(pattern))
        count_base = count_base.where(Book.title.ilike(pattern) | Book.author.ilike(pattern))

    total_result = await session.execute(count_base)
    total = total_result.scalar() or 0
    total_pages = max(1, (total + page_size - 1) // page_size)

    sort_column = ALLOWED_SORT_FIELDS[sort_by]
    order = sort_column.asc() if sort_order == "asc" else sort_column.desc()

    offset = (page - 1) * page_size
    result = await session.execute(
        base.order_by(order).offset(offset).limit(page_size)
    )
    books = result.scalars().all()

    return BookListResponse(
        items=[
            BookOut(
                id=str(b.id),
                title=b.title,
                author=b.author,
                language=b.language,
                book_type=b.book_type,
                status=b.status,
                total_chunks=b.total_chunks,
                total_pages=b.total_pages,
                minio_path=b.minio_path,
                created_at=b.created_at.isoformat() if b.created_at else "",
            )
            for b in books
        ],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
    )


@router.get("/{book_id}")
async def get_book(
    book_id: str,
    user: Annotated[User, Depends(get_current_user)] = None,
    session: Annotated[AsyncSession, Depends(get_db)] = None,
) -> BookDetailOut:
    result = await session.execute(
        select(Book).where(Book.id == uuid.UUID(book_id))
    )
    book = result.scalar_one_or_none()
    if book is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Book not found")
    if book.tenant_id != settings.DEFAULT_TENANT_ID:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Book not found")

    job_result = await session.execute(
        select(ProcessingJob)
        .where(ProcessingJob.book_id == uuid.UUID(book_id))
        .order_by(ProcessingJob.created_at.desc())
        .limit(1)
    )
    job = job_result.scalar_one_or_none()

    return BookDetailOut(
        id=str(book.id),
        title=book.title,
        author=book.author,
        language=book.language,
        book_type=book.book_type,
        status=book.status,
        total_chunks=book.total_chunks,
        total_pages=book.total_pages,
        minio_path=book.minio_path,
        created_at=book.created_at.isoformat() if book.created_at else "",
        job={
            "id": str(job.id),
            "status": job.status,
            "progress_pct": job.progress_pct,
            "current_step": job.current_step,
            "error_msg": job.error_msg,
            "started_at": job.started_at.isoformat() if job.started_at else None,
            "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        } if job else None,
    )


@router.get("/{book_id}/download")
async def download_book(
    book_id: str,
    user: Annotated[User, Depends(get_current_user)] = None,
    session: Annotated[AsyncSession, Depends(get_db)] = None,
) -> StreamingResponse:
    result = await session.execute(
        select(Book).where(Book.id == uuid.UUID(book_id))
    )
    book = result.scalar_one_or_none()
    if book is None or not book.minio_path:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Book not found")

    data = await storage.get_file(settings.MINIO_BUCKET_BOOKS, book.minio_path)
    filename = f"{book.title}.pdf"
    return StreamingResponse(
        iter([data]),
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


@router.get("/{book_id}/chunks")
async def list_book_chunks(
    book_id: str,
    user: Annotated[User, Depends(get_current_user)] = None,
) -> list[dict]:
    import meilisearch

    client = meilisearch.Client(settings.MEILISEARCH_URL, settings.MEILISEARCH_KEY)
    try:
        resp = client.index("documents").search(
            "",
            opt_params={
                "filter": [f"book_id={book_id}"],
                "limit": 200,
            },
        )
        hits = resp.get("hits", [])
        return [
            {
                "id": h.get("id"),
                "page": h.get("page"),
                "text_preview": (h.get("text") or "")[:200],
                "score": h.get("_rankingScore", 0),
            }
            for h in hits
        ]
    except Exception:
        return []


async def _delete_from_qdrant(book_id: str) -> None:
    from qdrant_client import AsyncQdrantClient
    from qdrant_client.models import FieldCondition, Filter, FilterSelector, MatchValue

    client = AsyncQdrantClient(host=settings.QDRANT_HOST, port=settings.QDRANT_PORT, timeout=10)
    try:
        await client.delete(
            collection_name="documents",
            points_selector=FilterSelector(
                filter=Filter(
                    must=[
                        FieldCondition(key="book_id", match=MatchValue(value=book_id)),
                        FieldCondition(key="tenant_id", match=MatchValue(value=settings.DEFAULT_TENANT_ID)),
                    ]
                )
            ),
        )
    finally:
        await client.close()


async def _delete_from_meilisearch(book_id: str) -> None:
    import meilisearch

    client = meilisearch.Client(settings.MEILISEARCH_URL, settings.MEILISEARCH_KEY)
    try:
        resp = client.index("documents").search(
            "",
            opt_params={
                "filter": [f"book_id={book_id}"],
                "limit": 1000,
            },
        )
        ids = [h["id"] for h in resp.get("hits", [])]
        if ids:
            client.index("documents").delete_documents(ids)
    except meilisearch.errors.MeilisearchApiError:
        pass

@router.put("/{book_id}", response_model=BookOut)
async def update_book(
    book_id: str,
    data: BookUpdate,
    user: Annotated[User, Depends(get_current_user)] = None,
    session: Annotated[AsyncSession, Depends(get_db)] = None,
) -> BookOut:
    result = await session.execute(
        select(Book).where(Book.id == uuid.UUID(book_id))
    )
    book = result.scalar_one_or_none()
    if book is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Book not found")

    if book.tenant_id != settings.DEFAULT_TENANT_ID:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Book not found")

    book.title = data.title
    book.author = data.author or None
    book.language = data.language
    book.book_type = data.book_type or None

    await session.commit()
    await session.refresh(book)

    # Concurrently update search index metadata if the book is ready
    if book.status == "ready":
        try:
            from qdrant_client import AsyncQdrantClient
            from qdrant_client.models import FieldCondition, Filter, MatchValue
            
            qdrant_client = AsyncQdrantClient(host=settings.QDRANT_HOST, port=settings.QDRANT_PORT, timeout=10)
            await qdrant_client.set_payload(
                collection_name="documents",
                payload={
                    "book_name": data.title,
                    "author": data.author or None,
                    "language": data.language,
                    "book_type": data.book_type or None,
                },
                points_selector=Filter(
                    must=[
                        FieldCondition(key="book_id", match=MatchValue(value=book_id)),
                        FieldCondition(key="tenant_id", match=MatchValue(value=settings.DEFAULT_TENANT_ID)),
                    ]
                )
            )
            await qdrant_client.close()
        except Exception as e:
            import logging
            logging.warning(f"Failed to update Qdrant points payload for book {book_id}: {e}")

        try:
            import meilisearch
            client = meilisearch.Client(settings.MEILISEARCH_URL, settings.MEILISEARCH_KEY)
            resp = client.index("documents").search(
                "",
                opt_params={
                    "filter": [f"book_id={book_id}"],
                    "limit": 1000,
                },
            )
            hits = resp.get("hits", [])
            if hits:
                updated_docs = []
                for h in hits:
                    doc = dict(h)
                    doc["book_name"] = data.title
                    doc["author"] = data.author or None
                    doc["language"] = data.language
                    doc["book_type"] = data.book_type or None
                    updated_docs.append(doc)
                client.index("documents").add_documents(updated_docs)
        except Exception as e:
            import logging
            logging.warning(f"Failed to update Meilisearch documents for book {book_id}: {e}")

    return BookOut(
        id=str(book.id),
        title=book.title,
        author=book.author,
        language=book.language,
        book_type=book.book_type,
        status=book.status,
        total_chunks=book.total_chunks,
        total_pages=book.total_pages,
        minio_path=book.minio_path,
        created_at=book.created_at.isoformat() if book.created_at else "",
    )


@router.delete("/{book_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def delete_book(
    book_id: str,
    user: Annotated[User, Depends(get_current_user)] = None,
    session: Annotated[AsyncSession, Depends(get_db)] = None,
) -> None:
    result = await session.execute(
        select(Book).where(Book.id == uuid.UUID(book_id))
    )
    book = result.scalar_one_or_none()
    if book is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Book not found")

    if book.tenant_id != settings.DEFAULT_TENANT_ID:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Book not found")

    # Delete from Qdrant and Meilisearch concurrently
    import asyncio

    await asyncio.gather(
        _delete_from_qdrant(book_id),
        _delete_from_meilisearch(book_id),
    )

    # Delete PDF from MinIO
    if book.minio_path:
        await storage.delete_file(settings.MINIO_BUCKET_BOOKS, book.minio_path)

    # Delete from PostgreSQL (cascades to ProcessingJob)
    await session.execute(sa_delete(Book).where(Book.id == uuid.UUID(book_id)))
    await session.commit()


@router.post("/{book_id}/reprocess", status_code=status.HTTP_202_ACCEPTED)
async def reprocess_book(
    book_id: str,
    user: Annotated[User, Depends(get_current_user)] = None,
    session: Annotated[AsyncSession, Depends(get_db)] = None,
) -> dict:
    result = await session.execute(
        select(Book).where(Book.id == uuid.UUID(book_id))
    )
    book = result.scalar_one_or_none()
    if book is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Book not found")

    if book.tenant_id != settings.DEFAULT_TENANT_ID:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Book not found")

    # Delete old processing jobs
    await session.execute(
        sa_delete(ProcessingJob).where(ProcessingJob.book_id == uuid.UUID(book_id))
    )

    # Reset book status
    book.status = "pending"
    book.total_chunks = 0
    book.total_pages = None

    # Create new job
    job_id = str(uuid.uuid4())
    job = ProcessingJob(
        id=uuid.UUID(job_id),
        book_id=uuid.UUID(book_id),
        tenant_id=book.tenant_id,
    )
    session.add(job)
    await session.commit()

    # Re-enqueue Celery task
    process_book.delay(book_id=str(book.id), minio_path=book.minio_path, tenant_id=book.tenant_id)

    return {"job_id": job_id, "book_id": book_id}
