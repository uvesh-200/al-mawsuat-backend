from __future__ import annotations

import asyncio
import json
from typing import AsyncGenerator

import redis.asyncio as aioredis

from app.config import settings

CHANNEL_JOBS = "events:jobs"
CHANNEL_BOOKS = "events:books"


async def _redis_pool() -> aioredis.Redis:
    return aioredis.from_url(settings.REDIS_URL, decode_responses=True)


async def publish_job_update(data: dict) -> None:
    r = await _redis_pool()
    try:
        await r.publish(CHANNEL_JOBS, json.dumps(data, default=str))
    finally:
        await r.aclose()


async def publish_book_update(data: dict) -> None:
    r = await _redis_pool()
    try:
        await r.publish(CHANNEL_BOOKS, json.dumps(data, default=str))
    finally:
        await r.aclose()


async def event_generator() -> AsyncGenerator[str, None]:
    r = await _redis_pool()
    pubsub = r.pubsub()
    await pubsub.subscribe(CHANNEL_JOBS, CHANNEL_BOOKS)
    try:
        while True:
            msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if msg is not None:
                channel = msg["channel"]
                data = msg["data"]
                if channel == CHANNEL_JOBS:
                    yield f"event: job-update\ndata: {data}\n\n"
                elif channel == CHANNEL_BOOKS:
                    yield f"event: book-update\ndata: {data}\n\n"
            else:
                yield ": keepalive\n\n"
    finally:
        await pubsub.unsubscribe(CHANNEL_JOBS, CHANNEL_BOOKS)
        await r.aclose()
