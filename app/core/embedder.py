import asyncio
import hashlib
import json
import logging

import httpx

from app.config import settings
from app.core.redis import get_redis

logger = logging.getLogger(__name__)

EMBED_BATCH_SIZE = 10
MAX_RETRIES = 3
RETRY_DELAY = 2.0

GEMINI_EMBED_URL = f"{settings.GEMINI_API_BASE}/v1/models/{settings.GEMINI_EMBEDDING_MODEL}:batchEmbedContents"


async def _embed_batch(client: httpx.AsyncClient, texts: list[str]) -> list[list[float]]:
    last_exc: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            requests = [
                {"model": f"models/{settings.GEMINI_EMBEDDING_MODEL}", "content": {"parts": [{"text": t}]}}
                for t in texts
            ]
            resp = await client.post(
                GEMINI_EMBED_URL,
                headers={"x-goog-api-key": settings.GEMINI_API_KEY},
                json={"requests": requests},
            )
            resp.raise_for_status()
            data = resp.json()
            return [e["values"] for e in data.get("embeddings", [])]
        except Exception as e:
            last_exc = e
            logger.warning("Embedding attempt %d/%d failed: %s", attempt + 1, MAX_RETRIES, e)
            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(RETRY_DELAY * (attempt + 1))
    logger.exception("All embedding retries exhausted")
    raise last_exc


async def embed_texts(texts: list[str]) -> list[list[float]]:
    vectors: list[list[float]] = []
    async with httpx.AsyncClient(timeout=300.0) as client:
        for i in range(0, len(texts), EMBED_BATCH_SIZE):
            batch = texts[i : i + EMBED_BATCH_SIZE]
            vectors.extend(await _embed_batch(client, batch))
    return vectors


async def embed_query(text: str, tenant_id: str) -> list[float]:
    cache_key = f"embed:{tenant_id}:{hashlib.sha256(text.encode()).hexdigest()}"
    r = await get_redis()
    cached = await r.get(cache_key)
    if cached is not None:
        return json.loads(cached)
    vectors = await embed_texts([text])
    vector = vectors[0]
    await r.setex(cache_key, 604800, json.dumps(vector))
    return vector
