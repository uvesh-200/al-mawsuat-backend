import asyncio
import hashlib
import json
import logging
import random

import httpx

from app.config import settings
from app.core.redis import get_redis

logger = logging.getLogger(__name__)

EMBED_BATCH_SIZE = 10
MAX_RETRIES = settings.OCR_MAX_RETRIES
BASE_DELAY = 1.0
MAX_DELAY = settings.OCR_RETRY_MAX_DELAY
CONCURRENT_BATCHES = settings.EMBED_CONCURRENT_BATCHES

GEMINI_EMBED_URL = f"{settings.GEMINI_API_BASE}/v1/models/{settings.GEMINI_EMBEDDING_MODEL}:batchEmbedContents"

_embed_semaphore = asyncio.Semaphore(CONCURRENT_BATCHES)


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
                timeout=180.0,
            )
            if resp.status_code == 429:
                retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
                wait = min(retry_after or BASE_DELAY * (2 ** min(attempt, 4)) + random.uniform(0, 1), MAX_DELAY)
                logger.warning("Embedding 429 rate limited, retrying in %.1fs (attempt %d/%d)", wait, attempt + 1, MAX_RETRIES)
                await asyncio.sleep(wait)
                last_exc = httpx.HTTPStatusError("429 Too Many Requests", request=resp.request, response=resp)
                continue
            resp.raise_for_status()
            data = resp.json()
            return [e["values"] for e in data.get("embeddings", [])]
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            last_exc = e
            wait = min(BASE_DELAY * (2 ** min(attempt, 4)) + random.uniform(0, 1), MAX_DELAY)
            logger.warning("Embedding attempt %d/%d failed (%s), retrying in %.1fs", attempt + 1, MAX_RETRIES, type(e).__name__, wait)
            await asyncio.sleep(wait)
        except httpx.HTTPStatusError as e:
            if e.response.status_code >= 500:
                last_exc = e
                wait = min(BASE_DELAY * (2 ** min(attempt, 4)) + random.uniform(0, 1), MAX_DELAY)
                logger.warning("Embedding attempt %d/%d failed (%d), retrying in %.1fs", attempt + 1, MAX_RETRIES, e.response.status_code, wait)
                await asyncio.sleep(wait)
            else:
                raise
        except Exception as e:
            last_exc = e
            logger.warning("Embedding attempt %d/%d failed: %s", attempt + 1, MAX_RETRIES, e)
            await asyncio.sleep(BASE_DELAY * (attempt + 1))
    logger.exception("All embedding retries exhausted")
    raise last_exc


async def embed_batch(client: httpx.AsyncClient, texts: list[str]) -> list[list[float]]:
    async with _embed_semaphore:
        return await _embed_batch(client, texts)


def _parse_retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


async def embed_texts(texts: list[str]) -> list[list[float]]:
    batches = [texts[i : i + EMBED_BATCH_SIZE] for i in range(0, len(texts), EMBED_BATCH_SIZE)]
    logger.info(
        "[TRACE] embed texts=%d batches=%d batch_size=%d model=%s",
        len(texts), len(batches), EMBED_BATCH_SIZE, settings.GEMINI_EMBEDDING_MODEL,
    )
    async with httpx.AsyncClient(timeout=300.0) as client:
        tasks = [embed_batch(client, batch) for batch in batches]
        results = await asyncio.gather(*tasks)
    vectors = []
    for r in results:
        vectors.extend(r)
    logger.info(
        "[TRACE] embed done vectors=%d dim=%d",
        len(vectors), len(vectors[0]) if vectors else 0,
    )
    return vectors


async def embed_query(text: str, tenant_id: str) -> list[float]:
    cache_key = f"embed:{tenant_id}:{hashlib.sha256(text.encode()).hexdigest()}"
    try:
        r = await asyncio.wait_for(get_redis(), timeout=5.0)
        cached = await asyncio.wait_for(r.get(cache_key), timeout=5.0)
        if cached is not None:
            return json.loads(cached)
    except Exception:
        logger.warning("Embedding cache read failed, computing fresh embedding")
    vectors = await embed_texts([text])
    vector = vectors[0]
    try:
        r = await asyncio.wait_for(get_redis(), timeout=5.0)
        await asyncio.wait_for(r.setex(cache_key, 604800, json.dumps(vector)), timeout=5.0)
    except Exception:
        logger.warning("Embedding cache write failed, skipping cache")
    return vector
