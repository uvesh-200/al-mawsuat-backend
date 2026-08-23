"""LangGraph state shared by all RAG nodes."""
from typing import Annotated, TypedDict


class AgentState(TypedDict):
    question: str
    tenant_id: str
    book_id: str | None
    language: str | None
    passages: list[dict]
    best_score: float
    top_vector_score: float
    retry_count: int
    answer: str
    sources: list[dict]
    no_result: bool
    streaming: bool
    embed_failed: bool

