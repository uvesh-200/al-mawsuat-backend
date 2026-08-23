import hashlib
import json

from app.core.redis import get_redis


def _normalise(question: str) -> str:
    return question.strip().lower()


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _cache_key(tenant_id: str, question: str, book_id: str | None) -> str:
    # book_id is part of the key: the same question scoped to a different
    # document must not reuse another document's cached retrieval/answer.
    scope = f"b:{book_id}" if book_id else "b:*"
    return f"cache:{tenant_id}:{scope}:{_hash(_normalise(question))}"


async def get_cached_answer(
    tenant_id: str, question: str, book_id: str | None = None
) -> dict | None:
    key = _cache_key(tenant_id, question, book_id)
    r = await get_redis()
    data = await r.get(key)
    if data is None:
        return None
    return json.loads(data)


async def set_cached_answer(
    tenant_id: str, question: str, response: dict, book_id: str | None = None
) -> None:
    key = _cache_key(tenant_id, question, book_id)
    r = await get_redis()
    await r.setex(key, 86400, json.dumps(response))
