from uuid import UUID

import fitz
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import select

from app.config import settings
from app.core.auth import get_optional_current_user
from app.models.db import AsyncSessionLocal
from app.models.tables import Book, User
from app.storage.minio_client import storage

router = APIRouter(tags=["highlight"])


@router.get("/highlight")
async def get_highlight(
    book_id: str = Query(...),
    page: int = Query(..., ge=1),
    bbox: str = Query(...),
    user: User | None = Depends(get_optional_current_user),
) -> Response:
    try:
        book_uuid = UUID(book_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid book_id format")

    user_tenant = user.tenant_id if user else settings.DEFAULT_TENANT_ID

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Book).where(Book.id == book_uuid))
        book = result.scalar_one_or_none()

    if book is None or book.tenant_id != user_tenant:
        raise HTTPException(status_code=403, detail="Book not found or access denied")

    if book.total_pages is not None and page > book.total_pages:
        raise HTTPException(
            status_code=400,
            detail=f"Page {page} exceeds total pages ({book.total_pages})",
        )

    try:
        parts = [float(x) for x in bbox.split(",")]
        if len(parts) != 4:
            raise ValueError
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="bbox must be 4 comma-separated numbers: x0,y0,x1,y1",
        )

    x0, y0, x1, y1 = parts
    cache_path = f"{settings.DEFAULT_TENANT_ID}/{book_id}/p{page}-{bbox}.png"

    cached = await storage.get_file_safe(settings.MINIO_BUCKET_HIGHLIGHTS, cache_path)
    if cached is not None:
        return Response(content=cached, media_type="image/png")

    if not book.minio_path:
        raise HTTPException(status_code=500, detail="Book PDF not found in storage")

    pdf_bytes = await storage.get_file(settings.MINIO_BUCKET_BOOKS, book.minio_path)
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        if page > len(doc):
            raise HTTPException(
                status_code=400,
                detail=f"Page {page} exceeds document pages ({len(doc)})",
            )

        p = doc[page - 1]
        rect = fitz.Rect(x0, y0, x1, y1)
        p.draw_rect(rect, color=(1, 0.85, 0), fill=(1, 0.85, 0), fill_opacity=0.45, width=0)
        pixmap = p.get_pixmap(dpi=150)
        img_bytes = pixmap.tobytes("png")
    finally:
        doc.close()

    await storage.upload_file(settings.MINIO_BUCKET_HIGHLIGHTS, cache_path, img_bytes, "image/png")

    return Response(content=img_bytes, media_type="image/png")
