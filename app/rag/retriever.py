import asyncio
import json
import logging

import meilisearch
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import FieldCondition, Filter, MatchValue

from app.config import settings

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
) -> list[dict]:
    try:
        loop = asyncio.get_running_loop()
        filters = [f"tenant_id = {tenant_id}"]
        if book_id:
            filters.append(f"book_id = {book_id}")

        def _search() -> dict:
            client = meilisearch.Client(settings.MEILISEARCH_URL, settings.MEILISEARCH_KEY, timeout=10)
            return client.index(MEILISEARCH_INDEX).search(
                query,
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
        results = await loop.run_in_executor(None, _search)
        hits = results.get("hits", [])
        logger.info(
            "Meilisearch keyword_search: filter=%s query=%r raw_count=%s",
            filters, query[:200], len(hits),
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
            "minio_path": h.get("minio_path"),
            "score": 0.5,
            "book_id": h.get("book_id"),
            "book_type": h.get("book_type"),
        }
        for h in hits
    ]
