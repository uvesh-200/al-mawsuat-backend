import asyncio
import logging
from typing import Annotated, TypedDict

from langgraph.graph import END, StateGraph
from openai import AsyncOpenAI

from app.config import settings
from app.core.embedder import embed_query
from app.core.translation import detect_language, translate_for_retrieval
from app.rag.reranker import rerank
from app.rag.retriever import keyword_search, vector_search

logger = logging.getLogger(__name__)

llm_client: AsyncOpenAI | None = None


def _get_llm_client() -> AsyncOpenAI:
    global llm_client
    if llm_client is None:
        llm_client = AsyncOpenAI(
            base_url=settings.GROQ_BASE_URL,
            api_key=settings.GROQ_API_KEY,
            timeout=240.0,
        )
    return llm_client


async def _chat_with_retry(client: AsyncOpenAI, **kwargs) -> str:
    for attempt in range(3):
        try:
            resp = await client.chat.completions.create(**kwargs)
            return resp.choices[0].message.content or ""
        except Exception as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status == 429 and attempt < 2:
                await asyncio.sleep(5 * (attempt + 1))
                continue
            raise


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
LANGUAGE_NAMES = {"ar": "Arabic", "ur": "Urdu", "en": "English"}
GROUNDING_PROMPT_SYSTEM = """\
You are an Islamic knowledge assistant specialising in the Deobandi tradition.
Answer ONLY using the passages provided. Do not use your own knowledge.

Rules:
1. If the passages do not answer the question, respond with exactly:
   No relevant information found in the provided sources.
   (This one sentence is always written in English, even if the question is
   in another language. Only the sentence itself; never comment on it.)
2. Cite every claim in the format [Book Name, Page X, Chapter Y], using ONLY
   the metadata shown for each passage. Never invent or guess a book name,
   page number, or chapter. If a field is missing, omit it from the citation
   (e.g. [Page 3]). Do not use the passage numbers in citations.
3. Your ENTIRE answer must be written in {language}. Do not switch languages
   and do not include Arabic or Urdu passages in your answer, except brief
   names or single terms when necessary.

PASSAGES:
{passages}"""

NO_RESULT_REFUSALS = {
    "ar": "لا توجد معلومات ذات صلة في المصادر المقدمة.",
    "ur": "فراہم کردہ ذرائع میں کوئی متعلقہ معلومات نہیں ملی۔",
}

_URDU_SPECIFIC = set("\u0679\u067A\u067B\u067C\u067D\u067E\u067F\u0680\u0688\u0689\u068A\u068B\u068C\u068D\u068E\u068F\u0691\u0692\u0693\u0694\u0695\u0696\u0697\u0698\u0699\u06A0\u06A1\u06A2\u06A3\u06A4\u06A5\u06A6\u06A7\u06A8\u06A9\u06AA\u06AB\u06AC\u06AD\u06AE\u06AF\u06B0\u06B1\u06B2\u06B3\u06B4\u06B5\u06B6\u06B7\u06B8\u06B9\u06BA\u06BB\u06BC\u06BD\u06BE\u06BF\u06C0\u06C1\u06C2\u06C3\u06C4\u06C5\u06C6\u06C7\u06C8\u06C9\u06CA\u06CB\u06CC\u06CD\u06CE\u06CF\u06D0\u06D1\u06D2\u06D3\u06D4\u06D5\u06D6\u06D7\u06D8\u06D9\u06DA\u06DB\u06DC\u06DD\u06DE\u06DF\u06E0\u06E1\u06E2\u06E3\u06E4\u06E5\u06E6\u06E7\u06E8\u06E9\u06EA\u06EB\u06EC\u06ED\u06EE\u06EF\u06F0\u06F1\u06F2\u06F3\u06F4\u06F5\u06F6\u06F7\u06F8\u06F9\u06FA\u06FB\u06FC\u06FD\u06FE\u06FF\u0640\u0626\u0624\u0671\u06C0")


def _answer_language(answer: str) -> str:
    import re

    latin = len(re.findall(r"[A-Za-z]", answer))
    arabic = len(re.findall(r"[\u0600-\u06FF]", answer))
    if arabic == 0:
        return "en"
    urdu_specific = sum(1 for ch in answer if ch in _URDU_SPECIFIC)
    if urdu_specific >= max(3, arabic // 10):
        return "ur"
    if latin > arabic:
        return "en"
    return "ar"


async def _enforce_language(client: AsyncOpenAI, answer: str, lang_name: str) -> str:
    try:
        rewritten = await _chat_with_retry(
            client,
            model=settings.GROQ_LLM_MODEL,
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Rewrite the following answer entirely in {lang_name}. "
                        "Keep the citations [Book Name, Page X, Chapter Y] exactly as they are. "
                        f"Answer:\n{answer}"
                    ),
                }
            ],
            temperature=0.2,
            max_tokens=256,
        )
        return rewritten.strip() or answer
    except Exception:
        logger.exception("Language rewrite failed, keeping original answer")
        return answer


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
        lines.append(f"Passage {i}: {text}\n    Source: {source}")
    return "\n\n".join(lines)


async def retrieve_node(state: AgentState) -> dict:
    question = state["question"]
    tenant_id = state["tenant_id"]
    lang = await detect_language(question)
    arabic_query = await translate_for_retrieval(question, lang)

    embed_query_text = arabic_query
    if lang != "ar" and arabic_query.strip() != question.strip():
        embed_query_text = f"{question} {arabic_query}"

    embed_failed = False
    try:
        vector = await embed_query(embed_query_text, tenant_id)
    except Exception:
        logger.exception("embed_query failed")
        embed_failed = True
        vector = None

    vs = []
    ks = []
    if vector is not None:
        vs_result, ks_result = await asyncio.gather(
            vector_search(vector, tenant_id, top_k=20),
            keyword_search(arabic_query, tenant_id, top_k=20),
            return_exceptions=True,
        )
        if not isinstance(vs_result, Exception):
            vs = vs_result
        else:
            logger.error("vector_search failed: %s", vs_result)
        if not isinstance(ks_result, Exception):
            ks = ks_result
        else:
            logger.error("keyword_search failed: %s", ks_result)
    else:
        try:
            ks = await keyword_search(arabic_query, tenant_id, top_k=20)
        except Exception as exc:
            logger.error("keyword_search fallback failed: %s", exc)
    combined = vs + ks

    reranked = await rerank(question, combined, top_k=10)
    best_score = reranked[0]["score"] if reranked else 0.0
    return {"passages": reranked, "best_score": best_score, "embed_failed": embed_failed, "language": lang}


def quality_check_node(state: AgentState) -> str:
    if state["passages"] and state["best_score"] >= 0.001:
        return "generate"
    if state["retry_count"] < 1:
        return "retry"
    return "no_result"


async def retry_node(state: AgentState) -> dict:
    client = _get_llm_client()
    try:
        rephrased = await _chat_with_retry(
            client,
            model=settings.GROQ_LLM_MODEL,
            messages=[{"role": "user", "content": f"Rephrase this question in Arabic: {state['question']}"}],
            temperature=0.7,
            max_tokens=256,
        )
    except Exception:
        logger.exception("LLM rephrase failed, keeping original question")
        rephrased = state["question"]
    return {"question": rephrased, "retry_count": state["retry_count"] + 1}


async def generate_node(state: AgentState) -> dict:
    client = _get_llm_client()
    lang = state.get("language") or await detect_language(state["question"])
    lang_name = LANGUAGE_NAMES.get(lang, "English")
    passages_text = _format_passages(state["passages"])
    prompt = GROUNDING_PROMPT_SYSTEM.format(passages=passages_text, language=lang_name)

    try:
        answer = await _chat_with_retry(
            client,
            model=settings.GROQ_LLM_MODEL,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": f"{state['question']}\n\nWrite your entire answer in {lang_name}."},
            ],
            temperature=0.3,
            max_tokens=256,
        )
        actual = _answer_language(answer)
        if actual != lang:
            answer = await _enforce_language(client, answer, lang_name)
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
    return {"answer": answer, "sources": sources}


def no_result_node(state: AgentState) -> dict:
    if state.get("embed_failed"):
        return {"answer": "The search service is temporarily unavailable. Please try again in a few minutes.", "no_result": True}
    lang = state.get("language") or "en"
    refusal = NO_RESULT_REFUSALS.get(lang, "No relevant information found in the provided sources.")
    if state.get("retry_count", 0) > 0:
        return {"answer": f"{refusal} Try rephrasing your question.", "no_result": True}
    return {"answer": refusal, "no_result": True}


graph = StateGraph(AgentState)
graph.add_node("retrieve", retrieve_node)
graph.add_node("retry", retry_node)
graph.add_node("generate", generate_node)
graph.add_node("no_result_handler", no_result_node)
graph.set_entry_point("retrieve")
graph.add_conditional_edges(
    "retrieve", quality_check_node, {"generate": "generate", "retry": "retry", "no_result": "no_result_handler"}
)
graph.add_conditional_edges(
    "retry", quality_check_node, {"generate": "generate", "no_result": "no_result_handler"}
)
graph.add_edge("generate", END)
graph.add_edge("no_result_handler", END)

rag_graph = graph.compile()
