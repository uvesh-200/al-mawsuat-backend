from redis.asyncio import Redis

from app.core.config import settings

_redis: Redis | None = None
_pubsub_redis: Redis | None = None


async def get_redis() -> Redis:
    global _redis
    if _redis is None:
        # Socket timeouts are mandatory: without them a Redis hiccup (fork/SAVE,
        # restart, network blip) makes every r.get()/setex() hang forever, and
        # some callers (cache lookups in /ask) are not covered by any
        # asyncio.wait_for — an infinite spinner for the user.
        _redis = Redis.from_url(
            settings.REDIS_URL,
            decode_responses=True,
            socket_connect_timeout=5.0,
            socket_timeout=5.0,
            health_check_interval=30,
        )
    return _redis


async def close_redis() -> None:
    global _redis
    if _redis is not None:
        try:
            await _redis.aclose()
        except RuntimeError:
            pass
        _redis = None


async def get_pubsub_redis() -> Redis:
    global _pubsub_redis
    if _pubsub_redis is None:
        _pubsub_redis = Redis.from_url(
            settings.REDIS_URL,
            decode_responses=False,
            socket_connect_timeout=5.0,
            socket_timeout=5.0,
            health_check_interval=30,
        )
    return _pubsub_redis
