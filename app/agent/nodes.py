import asyncio
from typing import Annotated, TypedDict

from openai import AsyncOpenAI

from app.config import settings
from app.core.translation import detect_language, translate_for_retrieval
from app.rag.embedder import embed_query
from app.rag.keyword_search import keyword_search
from app.rag.reranker import rerank
from app.rag.vector_search import vector_search

_llm_client: AsyncOpenAI | None = None


def _get_llm_client() -> AsyncOpenAI:
    global _llm_client
    if _llm_client is None:
        _llm_client = AsyncOpenAI(
            base_url=settings.VLLM_BASE_URL,
            api_key="not-needed",
        )
    return _llm_client


class AgentState(TypedDict):
    question: str
    tenant_id: str
    passages: list[dict]
    best_score: float
    retry_count: int
    answer: str
    sources: list[dict]
    no_result: bool
    streaming: bool


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


def _format_passages(passages: list[dict]) -> str:
    lines: list[str] = []
    for i, p in enumerate(passages, 1):
        text = p.get("text", "")
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

    lang = await detect_language(question)
    arabic_query = await translate_for_retrieval(question, lang)

    vector = await embed_query(arabic_query, tenant_id)

    vs, ks = await asyncio.gather(
        vector_search(vector, tenant_id, top_k=20),
        keyword_search(arabic_query, tenant_id, top_k=20),
    )

    combined = vs + ks
    reranked = rerank(question, combined, top_k=5)

    best_score = reranked[0]["score"] if reranked else 0.0

    return {
        "passages": reranked,
        "best_score": best_score,
    }


def quality_check_node(state: AgentState) -> str:
    if state["best_score"] >= 0.35:
        return "generate"
    if state["retry_count"] < 2:
        return "retry"
    return "no_result"


async def retry_node(state: AgentState) -> dict:
    client = _get_llm_client()
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

    resp = await client.chat.completions.create(
        model=settings.VLLM_MODEL,
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": state["question"]},
        ],
        temperature=0.3,
        max_tokens=1024,
    )
    answer = resp.choices[0].message.content or ""

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
    return {
        "answer": "No relevant information found in the provided sources.",
        "no_result": True,
    }
