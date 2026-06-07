import redis.asyncio as aioredis
from qdrant_client import AsyncQdrantClient
from sqlalchemy import text

from app.config import settings
from app.models.db import AsyncSessionLocal


async def check_postgres() -> str:
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
        return "ok"
    except Exception:
        return "error"


async def check_qdrant() -> str:
    try:
        client = AsyncQdrantClient(host=settings.QDRANT_HOST, port=settings.QDRANT_PORT, timeout=5)
        try:
            await client.get_collection("documents")
        except Exception:
            pass
        finally:
            await client.close()
        return "ok"
    except Exception:
        return "error"


async def check_redis() -> str:
    try:
        r = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
        await r.ping()
        await r.aclose()
        return "ok"
    except Exception:
        return "error"
