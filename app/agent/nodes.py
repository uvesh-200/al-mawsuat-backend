import asyncio
import logging
from typing import Annotated, TypedDict

from openai import AsyncOpenAI

from app.config import settings
from app.core.translation import detect_language, translate_for_retrieval
from app.rag.embedder import embed_query
from app.rag.keyword_search import keyword_search
from app.rag.reranker import rerank
from app.rag.vector_search import vector_search

logger = logging.getLogger(__name__)

_llm_client: AsyncOpenAI | None = None


def _get_llm_client() -> AsyncOpenAI:
    global _llm_client
    if _llm_client is None:
        _llm_client = AsyncOpenAI(
            base_url=settings.VLLM_BASE_URL,
            api_key="not-needed",
            timeout=240.0,
        )
    return _llm_client


class AgentState(TypedDict):
    question: str
    tenant_id: str
    language: str | None
    passages: list[dict]
    best_score: float
    retry_count: int
    answer: str
    sources: list[dict]
    no_result: bool
    streaming: bool
    embed_failed: bool


MAX_PASSAGE_TOKENS = 800
GROUNDING_PROMPT_SYSTEM = """\
You are an Islamic knowledge assistant specialising in the Deobandi tradition.
Answer ONLY using the passages provided. Do not use your own knowledge.
If the passages do not answer the question, say exactly:
"No relevant information found in the provided sources."
Always show original Arabic or Urdu text before any translation.
Cite every claim: [Book Name, Page X, Chapter Y]

PASSAGES:
{passages}

QUESTION: {question}"""


def _truncate_text(text: str, max_tokens: int = MAX_PASSAGE_TOKENS) -> str:
    words = text.split()
    if len(words) > max_tokens:
        return " ".join(words[:max_tokens]) + " ..."
    return text


def _format_passages(passages: list[dict]) -> str:
    lines: list[str] = []
    for i, p in enumerate(passages, 1):
        text = _truncate_text(p.get("text", ""))
        book = p.get("book_name", "")
        author = p.get("author", "")
        page = p.get("page_start", "")
        chapter = p.get("chapter", "")
        source = f"{book}" + (f", {author}" if author else "")
        if page:
            source += f", Page {page}"
        if chapter:
            source += f", {chapter}"
        lines.append(f"[{i}] {text}\n    — {source}")
    return "\n\n".join(lines)


async def retrieve_node(state: AgentState) -> dict:
    question = state["question"]
    tenant_id = state["tenant_id"]

    lang = state.get("language") or await detect_language(question)
    arabic_query = await translate_for_retrieval(question, lang)

    embed_failed = False
    try:
        vector = await embed_query(arabic_query, tenant_id)
    except Exception:
        logger.exception("embed_query failed")
        embed_failed = True
        vector = None

    if vector is not None:
        vs, ks = await asyncio.gather(
            vector_search(vector, tenant_id, top_k=20),
            keyword_search(arabic_query, tenant_id, top_k=20),
            return_exceptions=True,
        )

        if isinstance(vs, Exception):
            logger.error("vector_search failed: %s", vs)
            vs = []
        if isinstance(ks, Exception):
            logger.error("keyword_search failed: %s", ks)
            ks = []

        combined = vs + ks
    else:
        combined = []

    reranked = await rerank(question, combined, top_k=5)

    best_score = reranked[0]["score"] if reranked else 0.0

    return {
        "passages": reranked,
        "best_score": best_score,
        "embed_failed": embed_failed,
    }


def quality_check_node(state: AgentState) -> str:
    if state["best_score"] >= 0.35:
        return "generate"
    if state.get("embed_failed"):
        return "no_result"
    if state["retry_count"] < 1:
        return "retry"
    return "no_result"


async def retry_node(state: AgentState) -> dict:
    client = _get_llm_client()
    try:
        resp = await client.chat.completions.create(
            model=settings.VLLM_MODEL,
            messages=[
                {
                    "role": "user",
                    "content": f"Rephrase this question in Arabic: {state['question']}",
                },
            ],
            temperature=0.7,
            max_tokens=256,
        )
        rephrased = resp.choices[0].message.content or state["question"]
    except Exception:
        logger.exception("LLM rephrase failed, keeping original question")
        rephrased = state["question"]
    return {
        "question": rephrased,
        "retry_count": state["retry_count"] + 1,
    }


async def generate_node(state: AgentState) -> dict:
    client = _get_llm_client()
    passages_text = _format_passages(state["passages"])
    prompt = GROUNDING_PROMPT_SYSTEM.format(
        passages=passages_text,
        question=state["question"],
    )

    try:
        resp = await client.chat.completions.create(
            model=settings.VLLM_MODEL,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": state["question"]},
            ],
            temperature=0.3,
            max_tokens=512,
        )
        answer = resp.choices[0].message.content or ""
    except Exception:
        logger.exception("LLM generate failed, returning fallback")
        answer = "I encountered an error while generating the answer. Please try again."

    sources = [
        {
            "text": p.get("text", ""),
            "book_name": p.get("book_name", ""),
            "author": p.get("author", ""),
            "book_type": p.get("book_type"),
            "page_start": p.get("page_start"),
            "chapter": p.get("chapter"),
            "bbox": p.get("bbox"),
            "score": p.get("score", 0),
            "book_id": p.get("book_id"),
        }
        for p in state["passages"]
    ]

    return {
        "answer": answer,
        "sources": sources,
    }


def no_result_node(state: AgentState) -> dict:
    if state.get("embed_failed"):
        return {
            "answer": "The search service is temporarily unavailable due to high load on the CPU-based embedding server. Please try again in a few minutes.",
            "no_result": True,
        }
    if state.get("retry_count", 0) > 0:
        return {
            "answer": "After multiple attempts, no relevant information was found in the provided sources. Try rephrasing your question.",
            "no_result": True,
        }
    return {
        "answer": "No relevant information found in the provided sources.",
        "no_result": True,
    }
