from redis.asyncio import Redis

from app.config import settings

_redis: Redis | None = None
_pubsub_redis: Redis | None = None


async def get_redis() -> Redis:
    global _redis
    if _redis is None:
        _redis = Redis.from_url(settings.REDIS_URL, decode_responses=True)
    return _redis


async def get_pubsub_redis() -> Redis:
    global _pubsub_redis
    if _pubsub_redis is None:
        _pubsub_redis = Redis.from_url(settings.REDIS_URL, decode_responses=False)
    return _pubsub_redis
