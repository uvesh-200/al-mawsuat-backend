import redis.asyncio as redis

from app.config import settings

LOGIN_RATE_KEY_PREFIX = "rate:login:"
LOGIN_MAX_FAILURES = 5
LOGIN_WINDOW_SECONDS = 15 * 60


class LoginRateLimiter:
    """Track failed logins per IP in Redis (key: rate:login:{ip})."""

    def __init__(self, client: redis.Redis) -> None:
        self._redis = client

    def _key(self, ip: str) -> str:
        return f"{LOGIN_RATE_KEY_PREFIX}{ip}"

    async def is_blocked(self, ip: str) -> bool:
        raw = await self._redis.get(self._key(ip))
        if raw is None:
            return False
        try:
            return int(raw) >= LOGIN_MAX_FAILURES
        except ValueError:
            return False

    async def record_failure(self, ip: str) -> None:
        key = self._key(ip)
        count = await self._redis.incr(key)
        if count == 1:
            await self._redis.expire(key, LOGIN_WINDOW_SECONDS)

    async def reset(self, ip: str) -> None:
        await self._redis.delete(self._key(ip))


_redis: redis.Redis | None = None


async def get_redis() -> redis.Redis:
    global _redis
    if _redis is None:
        _redis = redis.from_url(settings.REDIS_URL, decode_responses=True)
    return _redis
