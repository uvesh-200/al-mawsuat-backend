from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.config import settings
from app.core.auth import get_optional_current_user
from app.core.redis import get_redis
from app.core.translation import detect_language
from app.models.schemas import AnswerResponse, SourceItem
from app.models.tables import User
from app.rag.agent import LLM_ERROR_FALLBACK, NO_RESULT_REFUSALS, rag_graph
from app.rag.cache import get_cached_answer, set_cached_answer

import logging

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ask", tags=["ask"])

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
    page = s.get("page_start")
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


@router.post("", response_model=AnswerResponse)
async def ask_json(
    body: AskRequest,
    user: Annotated[User | None, Depends(get_optional_current_user)] = None,
) -> AnswerResponse:
    tenant_id = user.tenant_id if user else "default"
    question = body.question
    book_id = body.book_id
    start = time.monotonic()
    logger.info(
        "[TRACE] ask start tenant=%s book_id=%s question=%r",
        tenant_id, book_id, question[:300],
    )

    cached = await _get_cached(tenant_id, question, book_id)
    if cached is not None:
        sources = [
            _build_source(s, i + 1, tenant_id)
            for i, s in enumerate(cached.get("sources", []))
        ]
        elapsed = int((time.monotonic() - start) * 1000)
        await _record_stats(tenant_id, was_cached=True, duration_ms=elapsed)
        return AnswerResponse(
            question=question,
            answer=cached["answer"],
            was_cached=True,
            no_result=cached.get("no_result", False),
            sources=sources,
        )

    try:
        result = await asyncio.wait_for(
            rag_graph.ainvoke(_initial_state(question, tenant_id, body.language, book_id)),
            timeout=280.0,
        )
    except asyncio.TimeoutError:
        elapsed = int((time.monotonic() - start) * 1000)
        await _record_stats(tenant_id, was_cached=False, duration_ms=elapsed)
        raise HTTPException(status_code=504, detail="Request timed out. Please try again later.")
    answer = _strip_placeholders(result.get("answer", ""))
    graph_no_result = result.get("no_result", False)
    no_result = graph_no_result or _looks_like_no_result(answer)
    if no_result and not graph_no_result:
        answer = await _localized_refusal(question)
    raw_sources = result.get("sources", [])

    sources = [
        _build_source(s, i + 1, tenant_id)
        for i, s in enumerate(raw_sources)
    ]

    cache_body = {
        "answer": answer,
        "no_result": no_result,
        "sources": raw_sources,
    }
    # Never cache LLM-error fallbacks: a transient provider outage would
    # otherwise serve the error for the whole cache TTL.
    if answer != LLM_ERROR_FALLBACK:
        await _set_cached(tenant_id, question, cache_body)

    elapsed = int((time.monotonic() - start) * 1000)
    await _record_stats(tenant_id, was_cached=False, duration_ms=elapsed)
    logger.info(
        "[TRACE] ask done tenant=%s elapsed_ms=%d no_result=%s cached=%s sources=%d answer=%r",
        tenant_id, elapsed, no_result, False, len(sources), answer[:300],
    )

    return AnswerResponse(
        question=question,
        answer=answer,
        no_result=no_result,
        sources=sources,
    )


@router.get("/stream")
async def ask_stream(
    question: str,
    language: str | None = None,
    book_id: str | None = None,
    user: Annotated[User | None, Depends(get_optional_current_user)] = None,
):
    tenant_id = user.tenant_id if user else "default"
    start = time.monotonic()

    if len(question) > 4096:
        return StreamingResponse(
            iter([f"data: {json.dumps({'type': 'error', 'content': 'Question too long (max 4096 characters)'})}\n\n"]),
            media_type="text/event-stream",
        )

    cached = await _get_cached(tenant_id, question, book_id)
    if cached is not None:
        answer = cached["answer"]
        no_result = cached.get("no_result", False)
        raw_sources = cached.get("sources", [])
        was_cached = True
        elapsed = int((time.monotonic() - start) * 1000)
        await _record_stats(tenant_id, was_cached=True, duration_ms=elapsed)
    else:
        try:
            result = await asyncio.wait_for(
                rag_graph.ainvoke(_initial_state(question, tenant_id, language, book_id)),
                timeout=280.0,
            )
        except asyncio.TimeoutError:
            elapsed = int((time.monotonic() - start) * 1000)
            await _record_stats(tenant_id, was_cached=False, duration_ms=elapsed)
            error_data = json.dumps({"type": "error", "content": "Request timed out. Please try again later."})
            done_data = json.dumps({"type": "done"})
            return StreamingResponse(
                iter([f"data: {error_data}\n\ndata: {done_data}\n\n"]),
                media_type="text/event-stream",
            )
        answer = _strip_placeholders(result.get("answer", ""))
        graph_no_result = result.get("no_result", False)
        no_result = graph_no_result or _looks_like_no_result(answer)
        if no_result and not graph_no_result:
            answer = await _localized_refusal(question)
        raw_sources = result.get("sources", [])
        was_cached = False
        cache_body = {
            "answer": answer,
            "no_result": no_result,
            "sources": raw_sources,
        }
        await _set_cached(tenant_id, question, cache_body, book_id)
        elapsed = int((time.monotonic() - start) * 1000)
        await _record_stats(tenant_id, was_cached=False, duration_ms=elapsed)

    sources = [
        _build_source(s, i + 1, tenant_id)
        for i, s in enumerate(raw_sources)
    ]

    async def _stream():
        if was_cached or no_result:
            yield f"data: {json.dumps({'type': 'token', 'content': answer})}\n\n"
        else:
            words = answer.split(" ")
            for i, word in enumerate(words):
                sep = " " if i > 0 else ""
                yield f"data: {json.dumps({'type': 'token', 'content': sep + word})}\n\n"

        yield f"data: {json.dumps({'type': 'no_result', 'no_result': no_result})}\n\n"
        yield f"data: {json.dumps({'type': 'sources', 'sources': [s.model_dump() for s in sources]})}\n\n"
        yield f"data: {json.dumps({'type': 'done'})}\n\n"

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
