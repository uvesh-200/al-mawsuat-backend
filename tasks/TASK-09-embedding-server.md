# TASK-09 — Embedding Inference Server

**Feature:** Convert text to vectors using bge-m3  
**Repo:** al-mawsuat-backend  
**Week:** 3  
**Depends on:** TASK-01

---

## Description

Write `inference/embedding_server.py`. This is a standalone FastAPI server that loads the bge-m3 model once on startup and exposes a single endpoint. It runs as a separate Docker service so the model stays loaded in memory and does not reload on every request.

The model to load: `BAAI/bge-m3` from HuggingFace using `sentence_transformers.SentenceTransformer`. Load it at module level (outside any function) so it loads once when the server starts.

Single endpoint: `POST /embed`

Request body:
```json
{ "texts": ["text one", "text two", "text three"] }
```

Response body:
```json
{ "vectors": [[0.23, -0.87, ...], [0.11, 0.54, ...], ...] }
```

Each vector is a list of 1024 floats. The number of vectors in the response equals the number of texts in the request.

Use `model.encode(texts, normalize_embeddings=True)` and convert the numpy array to a Python list before returning.

Process texts in batches of maximum 50 at a time internally to avoid memory spikes on large requests. If the request contains 200 texts, process them as 4 batches of 50 and concatenate results.

Add this service to `docker-compose.yml` as `embedding_server` running on internal port 8001. It must not be exposed externally — only accessible to other Docker services on the internal network.

Add a `GET /health` endpoint that returns `{"status": "ok", "model": "BAAI/bge-m3"}`.

---

## Acceptance criteria

- [ ] Server starts without error and logs that bge-m3 is loaded
- [ ] `POST /embed` with `{"texts": ["hello"]}` returns a vector of exactly 1024 floats
- [ ] `POST /embed` with 100 texts returns 100 vectors
- [ ] All vector values are floats between -1.0 and 1.0 (normalised)
- [ ] Arabic text `{"texts": ["الحمد لله"]}` returns a valid vector (not zeros or errors)
- [ ] Urdu text also produces a valid vector
- [ ] Sending 200 texts at once does not crash the server
- [ ] `GET /health` returns 200 with model name
- [ ] Server is reachable at `http://embedding_server:8001` from other Docker services
