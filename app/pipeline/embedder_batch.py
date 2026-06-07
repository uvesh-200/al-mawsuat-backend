import asyncio
import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

EMBED_BATCH_SIZE = 10
MAX_RETRIES = 3
RETRY_DELAY = 2.0


async def embed_chunks(chunks: list[dict]) -> list[list[float]]:
    texts = [c["text"] for c in chunks]
    vectors: list[list[float]] = []

    async with httpx.AsyncClient(timeout=300.0) as client:
        for i in range(0, len(texts), EMBED_BATCH_SIZE):
            batch = texts[i : i + EMBED_BATCH_SIZE]
            vectors.extend(await _embed_batch_with_retry(client, batch))

    return vectors


async def _embed_batch_with_retry(
    client: httpx.AsyncClient, batch: list[str]
) -> list[list[float]]:
    last_exc: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = await client.post(
                f"{settings.EMBEDDING_SERVER_URL}/embed",
                json={"texts": batch},
            )
            resp.raise_for_status()
            return resp.json()["vectors"]
        except Exception as e:
            last_exc = e
            logger.warning("Embedding attempt %d/%d failed: %s", attempt + 1, MAX_RETRIES, e)
            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(RETRY_DELAY * (attempt + 1))

    logger.exception("All embedding retries exhausted")
    raise last_exc  # type: ignore[misc]
