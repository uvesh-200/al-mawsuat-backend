import logging

import meilisearch

from app.config import settings

logger = logging.getLogger(__name__)

MEILISEARCH_INDEX = "documents"


async def keyword_search(
    query: str,
    tenant_id: str,
    top_k: int = 20,
) -> list[dict]:
    try:
        client = meilisearch.Client(settings.MEILISEARCH_URL, settings.MEILISEARCH_KEY, timeout=10)
        results = client.index(MEILISEARCH_INDEX).search(
            query,
            opt_params={
                "filter": [f"tenant_id = {tenant_id}"],
                "limit": top_k,
            },
        )
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
