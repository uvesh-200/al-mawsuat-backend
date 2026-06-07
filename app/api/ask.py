from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.agent.rag_agent import rag_graph
from app.core.auth import get_current_user
from app.models.schemas import AnswerResponse, SourceItem
from app.models.tables import User
from app.rag.cache import get_cached_answer, set_cached_answer

router = APIRouter(prefix="/ask", tags=["ask"])


class AskRequest(BaseModel):
    question: str = Field(min_length=1)
    language: str | None = None


def _build_source(
    s: dict, rank: int, tenant_id: str
) -> SourceItem:
    bbox = s.get("bbox")
    bbox_list: list[float] | None = None
    if bbox and isinstance(bbox, (list, tuple)):
        bbox_list = [float(v) for v in bbox]

    highlight_url = None
    book_id = s.get("book_id", "")
    page = s.get("page_start")
    if book_id and page is not None and bbox_list and len(bbox_list) == 4:
        bbox_str = f"{bbox_list[0]},{bbox_list[1]},{bbox_list[2]},{bbox_list[3]}"
        highlight_url = f"/highlight?book_id={book_id}&page={page}&bbox={bbox_str}"

    return SourceItem(
        rank=rank,
        book_id=book_id,
        book_name=s.get("book_name", ""),
        author=s.get("author"),
        book_type=s.get("book_type"),
        chapter=s.get("chapter"),
        page=page,
        relevance_score=s.get("score", 0.0),
        bbox=bbox_list,
        highlight_url=highlight_url,
    )


def _initial_state(question: str, tenant_id: str) -> dict:
    return {
        "question": question,
        "tenant_id": tenant_id,
        "passages": [],
        "best_score": 0.0,
        "retry_count": 0,
        "answer": "",
        "sources": [],
        "no_result": False,
        "streaming": False,
    }


@router.post("", response_model=AnswerResponse)
async def ask_json(
    body: AskRequest,
    user: Annotated[User, Depends(get_current_user)],
) -> AnswerResponse:
    tenant_id = user.tenant_id
    question = body.question

    cached = await get_cached_answer(tenant_id, question)
    if cached is not None:
        sources = [
            _build_source(s, i + 1, tenant_id)
            for i, s in enumerate(cached.get("sources", []))
        ]
        return AnswerResponse(
            question=question,
            answer=cached["answer"],
            was_cached=True,
            no_result=cached.get("no_result", False),
            sources=sources,
        )

    result = await rag_graph.ainvoke(_initial_state(question, tenant_id))
    answer = result.get("answer", "")
    no_result = result.get("no_result", False)
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
    await set_cached_answer(tenant_id, question, cache_body)

    return AnswerResponse(
        question=question,
        answer=answer,
        no_result=no_result,
        sources=sources,
    )


@router.get("/stream")
async def ask_stream(
    question: str,
    user: Annotated[User, Depends(get_current_user)],
):
    tenant_id = user.tenant_id

    cached = await get_cached_answer(tenant_id, question)
    if cached is not None:
        answer = cached["answer"]
        no_result = cached.get("no_result", False)
        raw_sources = cached.get("sources", [])
        was_cached = True
    else:
        result = await rag_graph.ainvoke(_initial_state(question, tenant_id))
        answer = result.get("answer", "")
        no_result = result.get("no_result", False)
        raw_sources = result.get("sources", [])
        was_cached = False
        cache_body = {
            "answer": answer,
            "no_result": no_result,
            "sources": raw_sources,
        }
        await set_cached_answer(tenant_id, question, cache_body)

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
