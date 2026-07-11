from __future__ import annotations

import uuid
from datetime import timezone
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.auth import get_current_user
from app.models.db import get_db
from app.models.tables import Book, ProcessingJob, User
from app.storage.minio_client import storage
from workers.celery_app import process_book

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

    await storage.upload_file(settings.MINIO_BUCKET_BOOKS, minio_path, data, file.content_type or "application/pdf")

    book = Book(
        id=uuid.UUID(book_id), tenant_id=tenant_id, title=title,
        author=author or None, language=language, book_type=book_type or None,
        minio_path=minio_path, uploaded_by=user.id if user else None,
    )
    session.add(book)
    job = ProcessingJob(id=uuid.UUID(job_id), book_id=uuid.UUID(book_id), tenant_id=tenant_id)
    session.add(job)
    await session.commit()

    process_book.delay(book_id=str(book.id), minio_path=minio_path, tenant_id=tenant_id)
    return UploadOut(job_id=job_id, book_id=book_id)


@router.get("", response_model=BookListResponse)
async def list_books(
    q: str = Query("", description="Search query (matches title and author)"),
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(20, ge=1, le=200, description="Items per page"),
    skip: int | None = Query(None, description="Skip N items (alternative to page)"),
    limit: int | None = Query(None, description="Limit items (alternative to page_size)"),
    sort_by: Literal["title", "author", "language", "book_type", "status", "total_chunks", "total_pages", "created_at"] = Query(
        "created_at", description="Field to sort by"
    ),
    sort_order: Literal["asc", "desc"] = Query("desc", description="Sort direction"),
    user: Annotated[User, Depends(get_current_user)] = None,
    session: Annotated[AsyncSession, Depends(get_db)] = None,
) -> BookListResponse:
    if skip is not None:
        page = (skip // max(page_size, 1)) + 1
    if limit is not None:
        page_size = limit

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
    result = await session.execute(base.order_by(order).offset(offset).limit(page_size))
    books = result.scalars().all()

    return BookListResponse(
        items=[BookOut(
            id=str(b.id), title=b.title, author=b.author, language=b.language,
            book_type=b.book_type, status=b.status, total_chunks=b.total_chunks,
            total_pages=b.total_pages, minio_path=b.minio_path,
            created_at=b.created_at.isoformat() if b.created_at else "",
        ) for b in books],
        total=total, page=page, page_size=page_size, total_pages=total_pages,
    )


@router.get("/{book_id}")
async def get_book(
    book_id: str,
    user: Annotated[User, Depends(get_current_user)] = None,
    session: Annotated[AsyncSession, Depends(get_db)] = None,
) -> BookDetailOut:
    result = await session.execute(select(Book).where(Book.id == uuid.UUID(book_id)))
    book = result.scalar_one_or_none()
    if book is None or book.tenant_id != settings.DEFAULT_TENANT_ID:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Book not found")

    job_result = await session.execute(
        select(ProcessingJob).where(ProcessingJob.book_id == uuid.UUID(book_id))
        .order_by(ProcessingJob.created_at.desc()).limit(1)
    )
    job = job_result.scalar_one_or_none()

    return BookDetailOut(
        id=str(book.id), title=book.title, author=book.author,
        language=book.language, book_type=book.book_type, status=book.status,
        total_chunks=book.total_chunks, total_pages=book.total_pages,
        minio_path=book.minio_path,
        created_at=book.created_at.isoformat() if book.created_at else "",
        job={
            "id": str(job.id), "status": job.status, "progress_pct": job.progress_pct,
            "current_step": job.current_step, "error_msg": job.error_msg,
            "started_at": job.started_at.isoformat() if job.started_at else None,
            "finished_at": job.finished_at.isoformat() if job.finished_at else None,
            "duration_seconds": int((job.finished_at - job.started_at).total_seconds())
                if job.started_at and job.finished_at else None,
        } if job else None,
    )


@router.get("/{book_id}/download")
async def download_book(
    book_id: str,
    user: Annotated[User, Depends(get_current_user)] = None,
    session: Annotated[AsyncSession, Depends(get_db)] = None,
) -> StreamingResponse:
    result = await session.execute(select(Book).where(Book.id == uuid.UUID(book_id)))
    book = result.scalar_one_or_none()
    if book is None or not book.minio_path:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Book not found")

    data = await storage.get_file(settings.MINIO_BUCKET_BOOKS, book.minio_path)
    return StreamingResponse(
        iter([data]), media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{book.title}.pdf"'},
    )
