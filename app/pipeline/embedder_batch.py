import httpx

from app.config import settings


EMBED_BATCH_SIZE = 25


async def embed_chunks(chunks: list[dict]) -> list[list[float]]:
    texts = [c["text"] for c in chunks]
    vectors: list[list[float]] = []

    async with httpx.AsyncClient(timeout=300.0) as client:
        for i in range(0, len(texts), EMBED_BATCH_SIZE):
            batch = texts[i : i + EMBED_BATCH_SIZE]
            resp = await client.post(
                f"{settings.EMBEDDING_SERVER_URL}/embed",
                json={"texts": batch},
            )
            resp.raise_for_status()
            vectors.extend(resp.json()["vectors"])

    return vectors
