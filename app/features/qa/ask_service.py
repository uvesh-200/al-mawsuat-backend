"""Ask pipeline services: refusal, source building, caching, stats."""

import asyncio
import json
import re
import time
from datetime import datetime, timezone
from typing import Annotated

from fastapi import Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.core.config import settings
from app.core.security import get_optional_current_user
from app.core.redis import get_redis
from app.core.translation import detect_language
from app.models.schemas import AnswerResponse, SourceItem
from app.models.tables import User
from app.features.qa.agent import LLM_ERROR_FALLBACK, NO_RESULT_REFUSALS, rag_graph
from app.features.qa.cache import get_cached_answer, set_cached_answer

import logging

logger = logging.getLogger(__name__)

NO_RESULT_PATTERNS = (
    "no relevant information found",
    "no relevant information was found",
    "after multiple attempts, no relevant information",
    "لا توجد معلومات ذات صلة",
    "لا توجد معلومات",
    "لا معلومات",
    "لم يتم العثور على معلومات",
    "کوئی متعلقہ معلومات نہیں",
)

_PLACEHOLDER_RE = re.compile(r"\[(?:Book Name|Page X|Chapter Y)[^\]]*\]")


def _strip_placeholders(answer: str) -> str:
    return _PLACEHOLDER_RE.sub("", answer or "").strip()


async def _localized_refusal(question: str) -> str:
    lang = await detect_language(question)
    return NO_RESULT_REFUSALS.get(lang, "No relevant information found in the provided sources.")


def _looks_like_no_result(answer: str) -> bool:
    lowered = (answer or "").strip().lower()
    return any(pattern in lowered for pattern in NO_RESULT_PATTERNS)


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4096)
    language: str | None = None
    book_id: str | None = None


# A stored bbox is only usable for page-image highlighting if it is a real
# PDF-point rectangle. The Gemini text-only OCR path used to emit synthetic
# "layout" boxes with coordinates that can exceed the page size by orders of
# magnitude (x up to ~17800 on a ~600pt-wide page); such boxes are dropped so
# the UI falls back to the render-time text-locate path instead of drawing a
# garbage rectangle over the page image.
def _plausible_bbox(bbox: list[float]) -> bool:
    if len(bbox) != 4:
        return False
    x0, y0, x1, y1 = bbox
    if not all(isinstance(v, (int, float)) for v in (x0, y0, x1, y1)):
        return False
    if x0 < 0 or y0 < 0 or x1 <= x0 or y1 <= y0:
        return False
    # Real OCR boxes live inside the PDF page's point space (a page is at most
    # a few thousand points wide/tall for large-format books); synthetic boxes
    # from the char-width heuristic go far beyond this.
    return x1 <= 3000 and y1 <= 3000


def _build_source(
    s: dict, rank: int, tenant_id: str
) -> SourceItem:
    page = s.get("page_start")
    bbox_raw = s.get("bbox")
    bbox_list: list[float] | None = None
    if bbox_raw is not None:
        if isinstance(bbox_raw, str):
            parts = bbox_raw.split(",")
            if len(parts) == 4:
                try:
                    bbox_list = [float(v) for v in parts]
                except (ValueError, TypeError):
                    bbox_list = None
        elif isinstance(bbox_raw, (list, tuple)):
            try:
                bbox_list = [float(v) for v in bbox_raw]
            except (ValueError, TypeError):
                bbox_list = None
    if bbox_list is not None and not _plausible_bbox(bbox_list):
        logger.warning(
            "Dropping implausible chunk bbox %s for book=%s page=%s (rank=%s); "
            "viewer will fall back to text locate",
            bbox_list, s.get("book_id"), page, rank,
        )
        bbox_list = None

    # Gate EVERY per-page box, not just the top-level one: the viewer prefers
    # page_bboxes entries over the chunk-level bbox, so an implausible entry
    # here reaches the /highlight endpoint verbatim and renders as a misplaced
    # or full-page band. Synthetic OCR geometry (char-width heuristics) is the
    # usual source of such entries.
    raw_page_bboxes = s.get("page_bboxes") or []
    page_bboxes = []
    for pb in raw_page_bboxes:
        if not isinstance(pb, dict) or not _plausible_bbox(pb.get("bbox")):
            logger.warning(
                "Dropping implausible per-page bbox entry %s for book=%s page=%s "
                "(rank=%s)", pb, s.get("book_id"), page, rank,
            )
            continue
        page_bboxes.append(pb)

    highlight_url = None
    book_id = str(s.get("book_id") or "")
    if book_id and page is not None:
        query = f"book_id={book_id}&page={page}"
        if bbox_list and len(bbox_list) == 4:
            bbox_str = f"{bbox_list[0]},{bbox_list[1]},{bbox_list[2]},{bbox_list[3]}"
            query += f"&bbox={bbox_str}"
        if s.get("text"):
            # The chunk head corresponds to the page_start page being
            # highlighted; the highlight endpoint uses this snippet to locate
            # the passage on the page image (tesseract LCS match) when the
            # stored bbox is missing or a synthetic full-page box.
            import urllib.parse

            snippet = " ".join(s["text"].split())[:600]
            query += f"&text={urllib.parse.quote(snippet)}"
        highlight_url = f"/highlight?{query}"

    return SourceItem(
        rank=rank,
        book_id=book_id,
        book_name=s.get("book_name", ""),
        author=s.get("author"),
        book_type=s.get("book_type"),
        chapter=s.get("chapter"),
        page=page,
        page_start=page,
        page_end=s.get("page_end"),
        page_offsets=s.get("page_offsets"),
        minio_path=s.get("minio_path"),
        relevance_score=s.get("score", 0.0),
        text=s.get("text") or None,
        bbox=bbox_list,
        page_bboxes=page_bboxes,
        highlight_url=highlight_url,
    )


async def _get_cached(
    tenant_id: str, question: str, book_id: str | None
) -> dict | None:
    """Bounded, non-fatal cache read: a slow/broken Redis must degrade to a
    cache miss (full RAG run) instead of hanging the request forever."""
    try:
        return await asyncio.wait_for(
            get_cached_answer(tenant_id, question, book_id), timeout=5.0
        )
    except asyncio.TimeoutError:
        logger.warning("Cache read timed out, treating as miss")
        return None
    except Exception:
        logger.exception("Cache read failed, treating as miss")
        return None


async def _set_cached(
    tenant_id: str, question: str, cache_body: dict, book_id: str | None = None
) -> None:
    """Bounded, best-effort cache write: never fail the user request because
    caching hiccuped."""
    try:
        await asyncio.wait_for(
            set_cached_answer(tenant_id, question, cache_body, book_id),
            timeout=5.0,
        )
    except asyncio.TimeoutError:
        logger.warning("Cache write timed out, skipping cache")
    except Exception:
        logger.exception("Cache write failed, skipping cache")


async def _record_stats(
    tenant_id: str, was_cached: bool, duration_ms: int
) -> None:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    year_week = datetime.now(timezone.utc).strftime("%Y-%W")
    try:
        r = await get_redis()
        await r.incr(f"stats:{tenant_id}:queries:{today}")
        await r.expire(f"stats:{tenant_id}:queries:{today}", 172800)
        await r.incr(f"stats:{tenant_id}:queries_week:{year_week}")
        await r.expire(f"stats:{tenant_id}:queries_week:{year_week}", 1209600)
        if was_cached:
            await r.incr(f"stats:{tenant_id}:cache_hits:{today}")
        else:
            await r.incr(f"stats:{tenant_id}:cache_misses:{today}")
        await r.expire(f"stats:{tenant_id}:cache_hits:{today}", 172800)
        await r.expire(f"stats:{tenant_id}:cache_misses:{today}", 172800)
        await r.incrby(f"stats:{tenant_id}:rt_sum:{today}", duration_ms)
        await r.incr(f"stats:{tenant_id}:rt_count:{today}")
        await r.expire(f"stats:{tenant_id}:rt_sum:{today}", 172800)
        await r.expire(f"stats:{tenant_id}:rt_count:{today}", 172800)
    except Exception:
        logger = __import__("logging").getLogger(__name__)
        logger.exception("Failed to record stats")


def _initial_state(
    question: str, tenant_id: str, language: str | None = None, book_id: str | None = None
) -> dict:
    return {
        "question": question,
        "tenant_id": tenant_id,
        "book_id": book_id,
        "language": language,
        "passages": [],
        "best_score": 0.0,
        "top_vector_score": 0.0,
        "retry_count": 0,
        "answer": "",
        "sources": [],
        "no_result": False,
        "streaming": False,
        "embed_failed": False,
    }


