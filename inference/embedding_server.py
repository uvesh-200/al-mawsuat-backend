import numpy as np
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from sentence_transformers import SentenceTransformer

BATCH_SIZE = 50

app = FastAPI(title="Embedding Server")

model = SentenceTransformer("BAAI/bge-m3")


class EmbedRequest(BaseModel):
    texts: list[str] = Field(min_length=1)


class EmbedResponse(BaseModel):
    vectors: list[list[float]]


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "model": "BAAI/bge-m3"}


@app.post("/embed", response_model=EmbedResponse)
async def embed(req: EmbedRequest) -> EmbedResponse:
    all_vectors: list[np.ndarray] = []
    for start in range(0, len(req.texts), BATCH_SIZE):
        batch = req.texts[start : start + BATCH_SIZE]
        vecs = model.encode(batch, normalize_embeddings=True)
        all_vectors.append(vecs)
    result = np.concatenate(all_vectors, axis=0)
    return EmbedResponse(vectors=result.tolist())
