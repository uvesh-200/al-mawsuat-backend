"""Hybrid retrieval: multi-query keyword + vector search + rerank."""
import asyncio
import logging

from app.core.config import settings
from app.core.embedder import embed_query
from app.core.translation import (
    detect_language,
    translate_for_retrieval,
    translate_to_english,
)
from app.features.qa.citations import _is_citation_question
from app.features.qa.passages import _truncate_text
from app.features.qa.reranker import (
    _query_terms,
    normalise_transliteration,
    rerank,
)
from app.features.qa.retriever import keyword_search, vector_search
from app.features.qa.state import AgentState

logger = logging.getLogger(__name__)

def _build_keyword_query(normalised: str, english_query: str, embed_query_text: str) -> str:
    """Combined keyword query (original-language + English terms, deduped)."""
    terms = _query_terms(normalised) + _query_terms(english_query)
    terms = list(dict.fromkeys(terms))
    return " ".join(terms) or english_query or embed_query_text


def _build_keyword_queries(
    normalised: str, english_query: str, embed_query_text: str
) -> tuple[str, str | None]:
    """Split keyword queries for the two keyword-search legs.

    Returns (primary, secondary): one leg per script so the Meilisearch
    `frequency` AND-over-anywhere-matches behaviour cannot zero the whole
    keyword leg with a word that only matches another book's chunks.
    """
    primary_terms = _query_terms(normalised)
    secondary_terms = _query_terms(english_query)
    primary = " ".join(primary_terms) or " ".join(secondary_terms) or english_query or embed_query_text
    secondary = " ".join(secondary_terms) or None
    if secondary and secondary == primary:
        secondary = None
    return primary, secondary


async def retrieve_node(state: AgentState) -> dict:
    question = state["question"]
    tenant_id = state["tenant_id"]
    book_id = state.get("book_id")
    lang = await detect_language(question)
    arabic_query = await translate_for_retrieval(question, lang)
    # Arabic/Urdu questions need an English leg too: the indexed chunk text is
    # English, so a purely Arabic overlap against it is 0 and the rerank score
    # collapses below the confidence threshold even when the vector search
    # found the right passage.
    english_query = await translate_to_english(question, lang)

    # Strip Arabic-transliteration marks (ā, ṣ, ʿ, …) so "Kinānah" matches the
    # indexed "Kinanah" in both the embedding query and the rerank overlap.
    normalised = normalise_transliteration(question)

    embed_parts = [arabic_query]
    for extra in (normalised, english_query):
        if extra.strip() and extra.strip() not in embed_parts:
            embed_parts.append(extra)
    embed_query_text = " ".join(embed_parts)
    keyword_query, keyword_alt_query = _build_keyword_queries(normalised, english_query, embed_query_text)

    logger.info(
        "[TRACE] retrieve_queries lang=%s arabic=%r english=%r keyword=%r keyword_alt=%r embed_query=%r",
        lang, arabic_query[:300], english_query[:300], keyword_query[:300],
        keyword_alt_query[:300] if keyword_alt_query else None, embed_query_text[:300],
    )

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
            vector_search(vector, tenant_id, top_k=20, book_id=book_id),
            keyword_search(keyword_query, tenant_id, top_k=20, book_id=book_id, alt_query=keyword_alt_query),
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
            ks = await keyword_search(keyword_query, tenant_id, top_k=20, book_id=book_id, alt_query=keyword_alt_query)
        except Exception as exc:
            logger.error("keyword_search fallback failed: %s", exc)
    combined = vs + ks

    # Capture the raw Qdrant cosine BEFORE rerank: rerank() overwrites each
    # result dict's "score" in place with the reranked value, so reading it
    # after would lose the raw similarity signal the quality gate needs.
    top_vector_score = max((r.get("score", 0.0) for r in vs), default=0.0)

    # Prefer the secondary query in the other script when available so the
    # overlap component can match either the Arabic or English chunk text.
    secondary_query = english_query if lang == "ar" else arabic_query
    reranked = await rerank(normalised, combined, top_k=8, arabic_query=secondary_query)
    best_score = reranked[0]["score"] if reranked else 0.0
    logger.info(
        "[TRACE] rerank_results count=%d", len(reranked),
    )
    for i, r in enumerate(reranked):
        logger.info(
            "[TRACE] rerank rank=%d score=%.4f book=%s page=%s chapter=%s preview=%r",
            i + 1, r["score"], r.get("book_name"), r.get("page_start"),
            r.get("chapter"), r.get("text", "")[:200],
        )
    logger.info(
        "retrieve: tenant=%s book_id=%s lang=%s vs_raw=%d ks_raw=%d combined=%d reranked=%d "
        "best_score=%.4f top_vector_score=%.4f",
        tenant_id, book_id, lang, len(vs), len(ks), len(combined), len(reranked),
        best_score, top_vector_score,
    )
    return {
        "passages": reranked,
        "best_score": best_score,
        "top_vector_score": top_vector_score,
        "embed_failed": embed_failed,
        "language": lang,
    }


