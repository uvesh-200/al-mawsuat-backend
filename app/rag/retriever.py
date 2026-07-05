import asyncio
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
    top_k: int = 20,
) -> list[dict]:
    client = AsyncQdrantClient(host=settings.QDRANT_HOST, port=settings.QDRANT_PORT, timeout=10)
    try:
        results = await client.search(
            collection_name=QDRANT_COLLECTION,
            query_vector=vector,
            limit=top_k,
            query_filter=Filter(
                must=[FieldCondition(key="tenant_id", match=MatchValue(value=tenant_id))]
            ),
            with_payload=True,
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
    top_k: int = 20,
) -> list[dict]:
    try:
        loop = asyncio.get_running_loop()
        def _search() -> dict:
            client = meilisearch.Client(settings.MEILISEARCH_URL, settings.MEILISEARCH_KEY, timeout=10)
            return client.index(MEILISEARCH_INDEX).search(
                query,
                opt_params={
                    "filter": [f"tenant_id = {tenant_id}"],
                    "limit": top_k,
                },
            )
        results = await loop.run_in_executor(None, _search)
        hits = results.get("hits", [])
    except Exception:
        logger.exception("Meilisearch keyword search failed")
        return []

    return [
        {
            "text": h.get("text", ""),
            "book_name": h.get("book_name", ""),
            "author": h.get("author", ""),
            "page_start": h.get("page_start"),
            "chapter": h.get("chapter"),
            "bbox": h.get("bbox"),
            "minio_path": h.get("minio_path"),
            "score": 0.5,
            "book_id": h.get("book_id"),
            "book_type": h.get("book_type"),
        }
        for h in hits
    ]
