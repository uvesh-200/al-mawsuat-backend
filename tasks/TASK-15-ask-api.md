# TASK-15 — Ask API Endpoint (JSON + SSE Streaming)

**Feature:** The main question-answering endpoint  
**Repo:** al-mawsuat-backend  
**Week:** 5  
**Depends on:** TASK-14, TASK-12

---

## Description

Write `app/api/ask.py`. This is the endpoint users interact with to ask questions. It must support two response modes — regular JSON and Server-Sent Events (SSE) streaming for real-time token-by-token output.

Both modes must check the Redis cache first. If a cached answer exists, return it immediately without running the RAG pipeline.

**`POST /ask`** — regular JSON response.

Request body: `{ "question": "string", "language": "string (optional)" }`

Steps:
1. Check Redis cache using `cache.get_cached_answer(tenant_id, question)`
2. If cache hit: return cached response with `was_cached: true`
3. If cache miss: run `rag_graph.ainvoke(...)` with streaming=False
4. Store result in Redis cache
5. Return `AnswerResponse`

**`GET /ask/stream`** — SSE streaming response.

Query parameter: `question=string`

Returns `StreamingResponse` with `media_type="text/event-stream"`. Streams events in this format:

```
data: {"type": "token", "content": "Riba"}

data: {"type": "token", "content": " is"}

data: {"type": "sources", "sources": [...]}

data: {"type": "done"}
```

Cache: for streaming, check cache first. If hit, emit all tokens from cached answer in rapid succession (still as SSE events), then emit sources and done. Do not skip SSE format just because it is cached — the frontend always expects SSE format on this endpoint.

**Pydantic schemas in `app/models/schemas.py`:**

`AnswerResponse`:
```python
question: str
answer: str
was_cached: bool
no_result: bool
sources: list[SourceItem]
```

`SourceItem`:
```python
rank: int
book_id: str
book_name: str
author: str | None
book_type: str | None
chapter: str | None
page: int
original_text: str
relevance_score: float
bbox: list[float]
highlight_url: str
```

The `highlight_url` must be constructed as: `/highlight?book_id={book_id}&page={page}&bbox={x0},{y0},{x1},{y1}`

Both endpoints require authentication (`Depends(get_current_user)`).

---

## Acceptance criteria

- [ ] `POST /ask` with `{"question": "ما حكم الربا؟"}` returns an `AnswerResponse` with non-empty `answer` and at least one source
- [ ] `sources` array contains `highlight_url` for every source
- [ ] Second identical request returns `was_cached: true` and responds in under 200ms
- [ ] `GET /ask/stream?question=...` returns `Content-Type: text/event-stream`
- [ ] SSE stream emits `token` events, then a `sources` event, then a `done` event — in that order
- [ ] Off-topic question returns `no_result: true` with the standard fallback message
- [ ] Both endpoints return HTTP 401 when called without a valid JWT
- [ ] `bbox` in `highlight_url` is a comma-separated string of 4 numbers matching the source's bbox field
