import asyncio
import logging

import numpy as np
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sentence_transformers import SentenceTransformer

logger = logging.getLogger(__name__)

BATCH_SIZE = 10

app = FastAPI(title="Embedding Server")

model = SentenceTransformer("BAAI/bge-m3")


class EmbedRequest(BaseModel):
    texts: list[str] = Field(min_length=1)


class EmbedResponse(BaseModel):
    vectors: list[list[float]]


def _encode(texts: list[str]) -> list[list[float]]:
    all_vectors: list[np.ndarray] = []
    for start in range(0, len(texts), BATCH_SIZE):
        batch = texts[start : start + BATCH_SIZE]
        vecs = model.encode(batch, normalize_embeddings=True)
        all_vectors.append(vecs)
    result = np.concatenate(all_vectors, axis=0)
    return result.tolist()


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "model": "BAAI/bge-m3"}


@app.post("/embed", response_model=EmbedResponse)
async def embed(req: EmbedRequest) -> EmbedResponse:
    try:
        vectors = await asyncio.to_thread(_encode, req.texts)
        return EmbedResponse(vectors=vectors)
    except Exception as e:
        logger.exception("Embedding failed")
        return JSONResponse(
            status_code=500,
            content={"error": "Embedding failed", "message": str(e)},
        )
