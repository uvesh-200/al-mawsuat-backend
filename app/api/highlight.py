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

    word = re.sub(r"[\u064B-\u0652\u0670\u0640\u060C\u061B\u061F\u0660-\u0669]", "", word)
    word = word.translate(str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ى": "ي", "ة": "ه", "ؤ": "و", "ئ": "ي"}))
    return re.sub(r"[^\u0621-\u064A\u0671-\u06FFA-Za-z0-9]", "", word).lower()


def _locate_bbox(words: list[dict], snippet: str, rtl: bool = True) -> list[float] | None:
    from collections import defaultdict

    snippet_words = [_norm_word(w) for w in snippet.split() if _norm_word(w)]
    if len(snippet_words) < 3:
        return None

    lines: dict[str, list[tuple[str, list[float]]]] = defaultdict(list)
    for w in words:
        norm = _norm_word(w.get("text", ""))
        if norm:
            lines.setdefault(w.get("line", 0), []).append((norm, w["bbox"]))
    for line_words in lines.values():
        line_words.sort(key=lambda t: t[1][0])

    def lcs(a: list[str], b: list[str]) -> list[tuple[int, int]]:
        m, n = len(a), len(b)
        dp = [[0] * (n + 1) for _ in range(m + 1)]
        for i in range(m - 1, -1, -1):
            for j in range(n - 1, -1, -1):
                if a[i] == b[j]:
                    dp[i][j] = dp[i + 1][j + 1] + 1
                else:
                    dp[i][j] = max(dp[i + 1][j], dp[i][j + 1])
        matches: list[tuple[int, int]] = []
        i = j = 0
        while i < m and j < n:
            if a[i] == b[j]:
                matches.append((i, j))
                i += 1
                j += 1
            elif dp[i + 1][j] >= dp[i][j + 1]:
                i += 1
            else:
                j += 1
        return matches

    def span_box(boxes: list[list[float]]) -> list[float]:
        return [
            min(b[0] for b in boxes),
            min(b[1] for b in boxes),
            max(b[2] for b in boxes),
            max(b[3] for b in boxes),
        ]

    stream: list[tuple[str, list[float]]] = []
    for line_words in sorted(lines.values(), key=lambda lw: min(t[1][1] for t in lw)):
        ordered = line_words[::-1] if rtl else line_words
        stream.extend(ordered)

    matches = lcs(snippet_words, [t[0] for t in stream])
    page_len = len(stream)
    min_required = max(3, int(0.5 * min(len(snippet_words), page_len) + 0.5))
    if len(matches) < min_required:
        return None
    boxes = [stream[j][1] for _, j in matches]
    return span_box(boxes)


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
            page_rect = p.rect
            # A legitimate per-page bbox always lies within the page rect,
            # even when the chunk covers most of a densely typeset page
            # (dense Arabic pages routinely exceed 35% of the page area).
            # The synthetic boxes from the text-only Gemini OCR path violated
            # the page bounds (e.g. x1 > 3000pt), so containment is the
            # correct plausibility invariant; anything outside the page rect
            # is untrustworthy and falls back to locating the snippet.
            inside_page = (
                rect.x0 >= 0
                and rect.y0 >= 0
                and rect.x1 <= page_rect.width + 0.01
                and rect.y1 <= page_rect.height + 0.01
                and rect.x1 > rect.x0
                and rect.y1 > rect.y0
            )
            if inside_page:
                p.draw_rect(
                    rect, color=(1, 0.85, 0), fill=(1, 0.85, 0), fill_opacity=0.45, width=0
                )
            else:
                parts = None
        if parts is None and text is not None:
            found = _locate_bbox(_words_from_tesseract(p), text, rtl=book.language != "en")
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
