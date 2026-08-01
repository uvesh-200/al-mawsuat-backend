from uuid import UUID

import fitz
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import select

from app.config import settings
from app.core.auth import get_optional_current_user
from app.models.db import AsyncSessionLocal
from app.models.tables import Book, User
from app.pipeline.extractor import _words_from_tesseract
from app.storage.minio_client import storage

router = APIRouter(tags=["highlight"])


def _norm_word(word: str) -> str:
    import re

    word = re.sub(r"[\u064B-\u0652\u0670\u0640]", "", word)
    word = word.translate(str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ى": "ي", "ة": "ه", "ؤ": "و", "ئ": "ي"}))
    return re.sub(r"[^\w\u0600-\u06FF]", "", word).lower()


def _locate_bbox(words: list[dict], snippet: str) -> list[float] | None:
    snippet_words = [w for w in snippet.split() if w.strip()]
    if len(snippet_words) < 3:
        return None
    norm = {_norm_word(w["text"]): w["bbox"] for w in words if _norm_word(w["text"])}
    matched: list[list[float]] = []
    for w in snippet_words:
        box = norm.get(_norm_word(w))
        if box:
            matched.append(box)
    if len(matched) < 3 or len(matched) / len(snippet_words) < 0.35:
        return None
    return [
        min(b[0] for b in matched),
        min(b[1] for b in matched),
        max(b[2] for b in matched),
        max(b[3] for b in matched),
    ]


@router.get("/highlight")
async def get_highlight(
    book_id: str = Query(...),
    page: int = Query(..., ge=1),
    bbox: str | None = Query(None),
    text: str | None = Query(None),
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

    if bbox is not None:
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
        cache_suffix = bbox
    elif text is not None:
        parts = None
        import hashlib

        cache_suffix = "t" + hashlib.sha1(text.encode()).hexdigest()[:16]
    else:
        parts = None
        cache_suffix = "full"

    cache_path = f"{settings.DEFAULT_TENANT_ID}/{book_id}/p{page}-{cache_suffix}.png"

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
        if parts is not None:
            rect = fitz.Rect(x0, y0, x1, y1)
            p.draw_rect(rect, color=(1, 0.85, 0), fill=(1, 0.85, 0), fill_opacity=0.45, width=0)
        elif text is not None:
            found = _locate_bbox(_words_from_tesseract(p), text)
            if found is not None:
                p.draw_rect(
                    fitz.Rect(found[0] - 2, found[1] - 2, found[2] + 2, found[3] + 2),
                    color=(1, 0.85, 0),
                    fill=(1, 0.85, 0),
                    fill_opacity=0.45,
                    width=0,
                )
        pixmap = p.get_pixmap(dpi=150)
        img_bytes = pixmap.tobytes("png")
    finally:
        doc.close()

    await storage.upload_file(settings.MINIO_BUCKET_HIGHLIGHTS, cache_path, img_bytes, "image/png")

    return Response(content=img_bytes, media_type="image/png")
