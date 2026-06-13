import logging

from qdrant_client import AsyncQdrantClient
from qdrant_client.models import FieldCondition, Filter, MatchValue

from app.config import settings

logger = logging.getLogger(__name__)

QDRANT_COLLECTION = "documents"

_client: AsyncQdrantClient | None = None


def _get_client() -> AsyncQdrantClient:
    global _client
    if _client is None:
        _client = AsyncQdrantClient(host=settings.QDRANT_HOST, port=settings.QDRANT_PORT, timeout=10)
    return _client


async def vector_search(
    vector: list[float],
    tenant_id: str,
    top_k: int = 20,
) -> list[dict]:
    client = _get_client()
    try:
        results = await client.search(
            collection_name=QDRANT_COLLECTION,
            query_vector=vector,
            limit=top_k,
            query_filter=Filter(
                must=[
                    FieldCondition(key="tenant_id", match=MatchValue(value=tenant_id)),
                ]
            ),
            with_payload=True,
        )
    except Exception:
        logger.exception("Qdrant vector search failed")
        return []

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
