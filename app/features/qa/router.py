"""QA HTTP endpoints: JSON ask + SSE stream."""

import asyncio
import json
import re
import time
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
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


from app.features.qa.ask_service import (
    AskRequest,
    _build_source,
    _get_cached,
    _initial_state,
    _localized_refusal,
    _looks_like_no_result,
    _plausible_bbox,
    _record_stats,
    _set_cached,
    _strip_placeholders,
)


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
