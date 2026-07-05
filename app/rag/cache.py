import hashlib
import json

from app.core.redis import get_redis


def _normalise(question: str) -> str:
    return question.strip().lower()


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


async def get_cached_answer(tenant_id: str, question: str) -> dict | None:
    key = f"cache:{tenant_id}:{_hash(_normalise(question))}"
    r = await get_redis()
    data = await r.get(key)
    if data is None:
        return None
    return json.loads(data)


async def set_cached_answer(tenant_id: str, question: str, response: dict) -> None:
    key = f"cache:{tenant_id}:{_hash(_normalise(question))}"
    r = await get_redis()
    await r.setex(key, 86400, json.dumps(response))
