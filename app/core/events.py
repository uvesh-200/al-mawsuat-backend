from __future__ import annotations

import json
from typing import AsyncGenerator

from app.core.redis import get_redis

# Events are namespaced per tenant so an admin connection only ever sees its
# own tenant's job/book updates (no cross-tenant SSE leak).
CHANNEL_JOBS_PREFIX = "events:jobs"
CHANNEL_BOOKS_PREFIX = "events:books"


def channel_jobs(tenant_id: str) -> str:
    return f"{CHANNEL_JOBS_PREFIX}:{tenant_id}"


def channel_books(tenant_id: str) -> str:
    return f"{CHANNEL_BOOKS_PREFIX}:{tenant_id}"


async def publish_job_update(data: dict, tenant_id: str) -> None:
    r = await get_redis()
    payload = dict(data)
    payload["tenant_id"] = tenant_id
    await r.publish(channel_jobs(tenant_id), json.dumps(payload, default=str))


async def publish_book_update(data: dict, tenant_id: str) -> None:
    r = await get_redis()
    payload = dict(data)
    payload["tenant_id"] = tenant_id
    await r.publish(channel_books(tenant_id), json.dumps(payload, default=str))


async def event_generator(tenant_id: str) -> AsyncGenerator[str, None]:
    r = await get_redis()
    pubsub = r.pubsub()
    cj = channel_jobs(tenant_id)
    cb = channel_books(tenant_id)
    await pubsub.subscribe(cj, cb)
    try:
        while True:
            msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if msg is not None:
                channel = msg["channel"]
                data = msg["data"]
                if channel == cj:
                    yield f"event: job-update\ndata: {data}\n\n"
                elif channel == cb:
                    yield f"event: book-update\ndata: {data}\n\n"
            else:
                yield ": keepalive\n\n"
    finally:
        await pubsub.unsubscribe(cj, cb)
