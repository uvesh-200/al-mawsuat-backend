import asyncio
import json
import logging

import meilisearch
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import FieldCondition, Filter, MatchValue

from app.core.config import settings

logger = logging.getLogger(__name__)

QDRANT_COLLECTION = "documents"
MEILISEARCH_INDEX = "documents"


async def vector_search(
    vector: list[float],
    tenant_id: str,
    top_k: int = 30,
    book_id: str | None = None,
) -> list[dict]:
    client = AsyncQdrantClient(host=settings.QDRANT_HOST, port=settings.QDRANT_PORT, timeout=10)
    try:
        must: list = [
            FieldCondition(key="tenant_id", match=MatchValue(value=tenant_id))
        ]
        # Single-document context: hard-scope the vector search to this
        # book's points at the Qdrant query level. Without this, the query
        # runs against the whole tenant's vector store and unrelated books
        # pollute the candidates (cross-document contamination).
        if book_id:
            must.append(FieldCondition(key="book_id", match=MatchValue(value=book_id)))
        logger.info(
            "Qdrant vector_search: collection=%s filter=%s limit=%s",
            QDRANT_COLLECTION,
            json.dumps([{"key": c.key, "match": c.match.value} for c in must]),
            top_k,
        )
        results = await client.search(
            collection_name=QDRANT_COLLECTION,
            query_vector=vector,
            limit=top_k,
            query_filter=Filter(must=must),
            with_payload=True,
        )
        logger.info(
            "Qdrant vector_search: raw_count=%s results=%s",
            len(results),
            json.dumps(
                [
                    {
                        "score": round(r.score, 4),
                        "book": r.payload.get("book_name"),
                        "page": r.payload.get("page_start"),
                    }
                    for r in results
                ],
                default=str,
            ),
        )
    except Exception:
        logger.exception("Qdrant vector search failed")
        return []
    finally:
        await client.close()

    return [
        {
            "text": r.payload.get("text", ""),
            "book_name": r.payload.get("book_name", ""),
            "author": r.payload.get("author", ""),
            "page_start": r.payload.get("page_start"),
            "page_end": r.payload.get("page_end"),
            "chapter": r.payload.get("chapter"),
            "bbox": r.payload.get("bbox"),
            "page_bboxes": r.payload.get("page_bboxes"),
            "page_offsets": r.payload.get("page_offsets"),
            "minio_path": r.payload.get("minio_path"),
            "score": r.score,
            "book_id": r.payload.get("book_id"),
            "book_type": r.payload.get("book_type"),
        }
        for r in results
    ]


async def keyword_search(
    query: str,
    tenant_id: str,
    top_k: int = 30,
    book_id: str | None = None,
    alt_query: str | None = None,
) -> list[dict]:
    """Meilisearch keyword leg.

    Runs the query as one or more independent searches and merges the hits
    (deduped by chunk id). Separate legs are required for cross-script queries:
    Meilisearch's `frequency` matching ANDs together every query word that has
    a match anywhere in the index, so a single mixed Arabic+English query is
    zeroed whenever one of its words only appears in another book's chunks
    (e.g. `sharia`). A leg per script keeps each search self-consistent.
    """
    queries = [query]
    if alt_query and alt_query != query:
        queries.append(alt_query)

    try:
        loop = asyncio.get_running_loop()
        filters = [f"tenant_id = {tenant_id}"]
        if book_id:
            filters.append(f"book_id = {book_id}")

        def _search(q: str) -> dict:
            client = meilisearch.Client(settings.MEILISEARCH_URL, settings.MEILISEARCH_KEY, timeout=10)
            return client.index(MEILISEARCH_INDEX).search(
                q,
                opt_params={
                    "filter": filters,
                    "limit": top_k,
                    # Default 'last' matching filters out documents that don't
                    # match the trailing terms of a long query, which turns the
                    # keyword leg into a no-op for full-sentence queries.
                    # 'frequency' ranks partial matches by how many query terms
                    # they contain and returns them, giving the hybrid leg real
                    # recall.
                    "matchingStrategy": "frequency",
                },
            )
        results = await asyncio.gather(*(loop.run_in_executor(None, _search, q) for q in queries))

        hits: list[dict] = []
        seen: set[str] = set()
        for result in results:
            for h in result.get("hits", []):
                key = str(h.get("id") or (h.get("book_id"), h.get("page_start"), h.get("page_end"), h.get("text")))
                if key in seen:
                    continue
                seen.add(key)
                hits.append(h)
            if len(hits) >= top_k:
                break
        hits = hits[:top_k]
        logger.info(
            "Meilisearch keyword_search: filter=%s queries=%r raw_count=%s",
            filters, [q[:200] for q in queries], len(hits),
        )
    except Exception:
        logger.exception("Meilisearch keyword search failed")
        return []

    return [
        {
            "text": h.get("text", ""),
            "book_name": h.get("book_name", ""),
            "author": h.get("author", ""),
            "page_start": h.get("page_start"),
            "page_end": h.get("page_end"),
            "chapter": h.get("chapter"),
            "bbox": h.get("bbox"),
            "page_bboxes": h.get("page_bboxes"),
            "page_offsets": h.get("page_offsets"),
            "minio_path": h.get("minio_path"),
            "score": 0.5,
            "book_id": h.get("book_id"),
            "book_type": h.get("book_type"),
        }
        for h in hits
    ]
