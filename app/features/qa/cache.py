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


async def invalidate_tenant_cache(tenant_id: str) -> None:
    """Drop every cached answer for a tenant.

    Called on book soft-delete/restore: otherwise a pre-delete cached answer
    (with its sources) or a post-delete cached refusal would be served for up
    to the 24h TTL regardless of the book's new visibility.
    """
    r = await get_redis()
    pattern = f"cache:{tenant_id}:*"
    batch: list[str] = []
    async for key in r.scan_iter(match=pattern, count=200):
        batch.append(key if isinstance(key, str) else key.decode())
        if len(batch) >= 500:
            await r.delete(*batch)
            batch.clear()
    if batch:
        await r.delete(*batch)
