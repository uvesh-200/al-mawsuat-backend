import meilisearch

from app.config import settings

MEILISEARCH_INDEX = "documents"


async def keyword_search(
    query: str,
    tenant_id: str,
    top_k: int = 20,
) -> list[dict]:
    client = meilisearch.Client(settings.MEILISEARCH_URL, settings.MEILISEARCH_KEY)
    results = client.index(MEILISEARCH_INDEX).search(
        query,
        filter=[f"tenant_id = {tenant_id}"],
        limit=top_k,
    )

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
        for h in results.get("hits", [])
    ]
