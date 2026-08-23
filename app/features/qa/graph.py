"""LangGraph assembly: quality gate, retry, generation, refusals."""
import logging

from langgraph.graph import END, StateGraph

from app.core.config import settings
from app.core.translation import detect_language
from app.features.qa.citations import (
    _answer_language,
    _is_citation_question,
    _resolve_citations,
    _validate_citations,
)
from app.features.qa.llm import _chat_with_retry
from app.features.qa.passages import _format_passages
from app.features.qa.prompts import (
    GROUNDING_PROMPT_SYSTEM,
    LANGUAGE_NAMES,
    LLM_ERROR_FALLBACK,
    NO_RESULT_REFUSALS,
)
from app.features.qa.retrieval import retrieve_node
from app.features.qa.state import AgentState

logger = logging.getLogger(__name__)


# --- language enforcement + consistency check ---

async def _enforce_language(answer: str, lang_name: str) -> str:
    try:
        rewritten = await _chat_with_retry(
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Rewrite the following answer entirely in {lang_name}. "
                        "Keep all citation brackets [Pn, Page X] exactly as they "
                        "appear in the original answer. "
                        "Do not add, modify, or remove any citations."
                        f"Answer:\n{answer}"
                    ),
                }
            ],
            temperature=0.2,
            max_tokens=1024,
        )
        return rewritten.strip() or answer
    except Exception:
        logger.exception("Language rewrite failed, keeping original answer")
        return answer



async def _consistency_check(
    question: str, answer: str, passages_text: str
) -> str:
    """For multi-source answers: ask the LLM whether the cited passages agree.

    Returns the answer unchanged, or with a contradiction note prepended.
    Only called when ≥ 2 distinct passages are cited.
    """
    cited_indices = sorted(set(_iter_tag_indices(answer)))
    if len(cited_indices) < 2:
        return answer
    try:
        verdict = await _chat_with_retry(
            messages=[
                {
                    "role": "system",
                    "content": CONSISTENCY_CHECK_PROMPT,
                },
                {
                    "role": "user",
                    "content": (
                        f"Question: {question}\n\n"
                        f"Answer: {answer}\n\n"
                        f"Passages:\n{passages_text}"
                    ),
                },
            ],
            temperature=0.1,
            max_tokens=160,
        )
        verdict = verdict.strip()
        logger.info("[TRACE] consistency_check verdict=%r", verdict)
        if verdict.upper().startswith("CONTRADICT"):
            note = (
                "⚠️ Note: the source passages contain a disagreement on this point. "
                "Each passage's claim is stated separately below.\n\n"
            )
            return note + answer
    except Exception:
        logger.exception("Consistency check failed, returning answer as-is")
    return answer


# --- nodes ---------------------------------------------------------------


def quality_check_node(state: AgentState) -> str:
    """Route to generation only when retrieval is confident; otherwise retry
    once, then refuse.

    Two independent confidence signals, combined with OR:

    * ``best_score >= RAG_MIN_CONFIDENCE_SCORE`` — the reranked hybrid score
      (RRF + lexical overlap + entity boost). This is the historical gate; it
      carries Arabic-content matches whose evidence is lexical (vector
      similarity for Arabic chunks is weak in this corpus).
    * ``top_vector_score >= RAG_VECTOR_MIN_CONFIDENCE`` — the raw Qdrant
      cosine of the top vector hit. English-content matches score 0.65-0.78
      even when a long question dilutes the lexical overlap below the rerank
      threshold. This rescued genuine hits like the "which two Qur'anic
      verses..." question that previously returned an empty source list.

    Citation/source-reference questions ("what is the source reference for
    Ibn Hajar's comment on the Qiblah") use a lower vector bar: the correct
    chunk is nearly always found (vector ~0.62) but the question's words
    barely overlap the answer text, so a normal gate falsely refuses them.
    They are gate-relaxed but never retrieval-relaxed.

    Unrelated-but-lexically-overlapping noise (e.g. a riba question matching
    an unrelated Arabic chunk) sits at ~0.27 rerank and ~0.55 vector — below
    both relaxed signals — and is refused.
    """
    threshold = settings.RAG_MIN_CONFIDENCE_SCORE
    vector_min = settings.RAG_VECTOR_MIN_CONFIDENCE
    if _is_citation_question(state.get("question", "")):
        vector_min = settings.RAG_CITATION_VECTOR_MIN_CONFIDENCE
    best_score = state.get("best_score", 0.0)
    top_vector_score = state.get("top_vector_score", 0.0)
    confident = best_score >= threshold or top_vector_score >= vector_min
    if state["passages"] and confident:
        decision = "generate"
    elif state["retry_count"] < 1:
        decision = "retry"
    else:
        decision = "no_result"
    logger.info(
        "quality_gate: best_score=%.4f threshold=%.2f top_vector_score=%.4f vector_min=%.2f "
        "passages=%d citation_question=%s -> %s",
        best_score, threshold, top_vector_score, vector_min, len(state["passages"]),
        _is_citation_question(state.get("question", "")), decision,
    )
    return decision


async def retry_node(state: AgentState) -> dict:
    """One reformulation attempt before giving up. The rephrase is produced in
    the question's own language — the old unconditional Arabic rephrase
    mangled English questions on their second pass."""
    try:
        lang = state.get("language") or await detect_language(state["question"])
        lang_name = LANGUAGE_NAMES.get(lang, "English")
        rephrased = await _chat_with_retry(
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Rephrase this question in {lang_name} as a clear, "
                        f"concise search query: {state['question']}"
                    ),
                }
            ],
            temperature=0.7,
            max_tokens=256,
        )
    except Exception:
        logger.exception("LLM rephrase failed, keeping original question")
        rephrased = state["question"]
    return {"question": rephrased, "retry_count": state["retry_count"] + 1}


async def generate_node(state: AgentState) -> dict:
    lang = state.get("language") or await detect_language(state["question"])
    lang_name = LANGUAGE_NAMES.get(lang, "English")
    passages = state["passages"]
    passages_text = _format_passages(passages)

    prompt = GROUNDING_PROMPT_SYSTEM.format(
        passages=passages_text,
        language=lang_name,
        n_passages=len(passages),
    )
    logger.info("[TRACE] generate prompt for question=%r language=%s", state["question"], lang_name)
    logger.info("[TRACE] generate full prompt:\n%s", prompt)

    try:
        answer = await _chat_with_retry(
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": f"{state['question']}\n\nWrite your entire answer in {lang_name}."},
            ],
            temperature=0.3,
            max_tokens=1024,
        )

        # Post-generation citation validation
        bad_indices = _validate_citations(answer, passages)
        if bad_indices:
            logger.warning(
                "Generated answer contained out-of-range citation tags %s "
                "(passage count: %d). Re-generating with stricter prompt.",
                bad_indices, len(passages),
            )
            stricter_prompt = prompt + (
                f"\n\nIMPORTANT: Your previous answer used invalid citation tags "
                f"{bad_indices}. You only have {len(passages)} passages ([P1]–"
                f"[P{len(passages)}]). Only use tags in that range."
            )
            answer = await _chat_with_retry(
                messages=[
                    {"role": "system", "content": stricter_prompt},
                    {"role": "user", "content": f"{state['question']}\n\nWrite your entire answer in {lang_name}."},
                ],
                temperature=0.2,
                max_tokens=1024,
            )

        # Multi-source consistency check
        answer = await _consistency_check(state["question"], answer, passages_text)

        # Resolve [Pn] tags → canonical [Book, Page N] strings for the user
        answer = _resolve_citations(answer, passages)

        actual = _answer_language(answer)
        if actual != lang:
            answer = await _enforce_language(answer, lang_name)

    except Exception:
        logger.exception("LLM generate failed, returning fallback")
        answer = LLM_ERROR_FALLBACK

    logger.info("[TRACE] generate answer=%r", answer[:1000])

    sources = [
        {
            "text": p.get("text", ""),
            "book_name": p.get("book_name", ""),
            "author": p.get("author", ""),
            "book_type": p.get("book_type"),
            "page_start": p.get("page_start"),
            "page_end": p.get("page_end"),
            "page_bboxes": p.get("page_bboxes"),
            "page_offsets": p.get("page_offsets"),
            "minio_path": p.get("minio_path"),
            "chapter": p.get("chapter"),
            "bbox": p.get("bbox"),
            "score": p.get("score", 0),
            "book_id": p.get("book_id"),
            "relevance_score": p.get("score", 0),
        }
        for p in passages
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
