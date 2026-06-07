from __future__ import annotations

import uuid
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile, status
from pydantic import BaseModel
from sqlalchemy import delete as sa_delete, select
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
    status: str
    total_chunks: int
    created_at: str


class UploadOut(BaseModel):
    job_id: str
    book_id: str
    status: str = "queued"


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


@router.get("")
async def list_books(
    user: Annotated[User, Depends(get_current_user)] = None,
    session: Annotated[AsyncSession, Depends(get_db)] = None,
) -> list[BookOut]:
    result = await session.execute(
        select(Book)
        .where(Book.tenant_id == settings.DEFAULT_TENANT_ID)
        .order_by(Book.created_at.desc())
    )
    books = result.scalars().all()
    return [
        BookOut(
            id=str(b.id),
            title=b.title,
            author=b.author,
            language=b.language,
            status=b.status,
            total_chunks=b.total_chunks,
            created_at=b.created_at.isoformat() if b.created_at else "",
        )
        for b in books
    ]


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
