import hashlib
import json
import logging

import httpx

from app.config import settings
from app.rag.cache import _get_redis

logger = logging.getLogger(__name__)


async def embed_query(text: str, tenant_id: str) -> list[float]:
    cache_key = f"embed:{tenant_id}:{hashlib.sha256(text.encode()).hexdigest()}"
    r = await _get_redis()

    cached = await r.get(cache_key)
    if cached is not None:
        return json.loads(cached)

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{settings.EMBEDDING_SERVER_URL}/embed",
                json={"texts": [text]},
            )
            resp.raise_for_status()
            vector = resp.json()["vectors"][0]
    except Exception:
        logger.exception("Embedding server call failed")
        raise

    await r.setex(cache_key, 604800, json.dumps(vector))
    return vector
