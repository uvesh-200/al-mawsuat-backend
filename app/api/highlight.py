import hashlib
import logging
from uuid import UUID

import fitz
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import select

from app.core.config import settings
from app.core.security import get_optional_current_user
from app.core.db import AsyncSessionLocal
from app.models.tables import Book, User
from app.pipeline.extractor import _words_from_tesseract
from app.core.storage import storage

logger = logging.getLogger(__name__)

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


_HIGHLIGHT_COLOR = (1, 0.85, 0)


def render_highlight(
    page: "fitz.Page",
    bbox_list: list[float] | None,
    text: str | None,
    rtl: bool,
) -> tuple[bytes, dict]:
    """Draw the highlight band onto an open fitz page and rasterise it.

    Returns ``(png_bytes, meta)``. ``meta`` records what happened so callers
    can decide what is safe to cache and logs stay truthful:

    - ``mode="bbox"``: the stored rectangle was drawn (contained in the page).
    - ``mode="located"``: the box was rejected/absent and the snippet was
      located on the rendered page via OCR + LCS; that rectangle was drawn.
    - ``mode="none"``: nothing could be drawn — the stored box was implausible
      AND the snippet locate failed. The caller must NOT cache this render
      permanently (the old behaviour cached it under the bbox key, silently
      serving a no-highlight image for every later request with that box).
    - ``mode="full"``: no highlight requested — plain page view.

    Every degradation path logs loudly instead of failing silently.
    """
    meta: dict = {"mode": "full", "rect": None}
    used_bbox = bbox_list
    bbox_was_requested = bbox_list is not None
    if used_bbox is not None:
        rect = fitz.Rect(*used_bbox[:4])
        page_rect = page.rect
        # A legitimate per-page bbox always lies within the page rect, even
        # when the chunk covers most of a densely typeset page (dense Arabic
        # pages routinely exceed 35% of the page area). Synthetic boxes from
        # the text-only Gemini OCR path violated the page bounds (x1 in the
        # thousands), so containment is the plausibility invariant.
        inside_page = (
            rect.x0 >= 0
            and rect.y0 >= 0
            and rect.x1 <= page_rect.width + 0.01
            and rect.y1 <= page_rect.height + 0.01
            and rect.x1 > rect.x0
            and rect.y1 > rect.y0
        )
        if inside_page:
            page.draw_rect(
                rect, color=_HIGHLIGHT_COLOR, fill=_HIGHLIGHT_COLOR,
                fill_opacity=0.45, width=0,
            )
            meta = {"mode": "bbox", "rect": [rect.x0, rect.y0, rect.x1, rect.y1]}
        else:
            logger.warning(
                "Highlight bbox %s rejected for page %s: outside page rect "
                "(%sx%s); falling back to text locate",
                used_bbox, page.number + 1, round(page_rect.width, 1),
                round(page_rect.height, 1),
            )
            used_bbox = None

    if used_bbox is None and text is not None:
        found = _locate_bbox(_words_from_tesseract(page), text, rtl=rtl)
        if found is not None:
            page.draw_rect(
                fitz.Rect(found[0] - 2, found[1] - 2, found[2] + 2, found[3] + 2),
                color=_HIGHLIGHT_COLOR,
                fill=_HIGHLIGHT_COLOR,
                fill_opacity=0.45,
                width=0,
            )
            meta = {"mode": "located", "rect": [round(v, 2) for v in found]}
        else:
            logger.warning(
                "Highlight text locate failed for page %s (snippet %r…): "
                "rendering page without highlight and skipping cache",
                page.number + 1, " ".join(text.split())[:80],
            )
            meta = {"mode": "none", "reason": "bbox rejected and text locate failed"}

    if bbox_was_requested and meta["mode"] == "full":
        # a bbox was requested but nothing was drawn (rejected, no snippet)
        meta = {"mode": "none", "reason": "bbox rejected, no snippet to locate"}

    return page.get_pixmap(dpi=150).tobytes("png"), meta


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

    parts: list[float] | None = None
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
        cache_suffix = bbox
    elif text is not None:
        cache_suffix = "t" + hashlib.sha1(text.encode()).hexdigest()[:16]
    else:
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

        img_bytes, meta = render_highlight(
            doc[page - 1], parts, text, rtl=book.language != "en",
        )
    finally:
        doc.close()

    # Cache only renders that drew something or are legitimate plain page
    # views. A "none" render (implausible box + failed locate) must not be
    # persisted under the request's bbox key: it would silently serve a
    # no-highlight image for every future request carrying that box.
    if meta["mode"] in ("bbox", "located", "full"):
        await storage.upload_file(
            settings.MINIO_BUCKET_HIGHLIGHTS, cache_path, img_bytes, "image/png"
        )
    else:
        logger.warning(
            "Highlight render for book=%s page=%s produced no highlight; "
            "response served uncached",
            book_id, page,
        )

    return Response(content=img_bytes, media_type="image/png")
