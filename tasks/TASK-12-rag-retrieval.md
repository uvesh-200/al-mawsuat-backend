# TASK-12 — RAG Retrieval (Vector + Keyword + Reranking)

**Feature:** Search uploaded books for relevant passages  
**Repo:** al-mawsuat-backend  
**Week:** 4  
**Depends on:** TASK-10

---

## Description

Write the three retrieval modules and the cache layer. Together these form the "retrieve" half of the RAG pipeline. No LLM is involved in this task — only finding the most relevant passages from the indexed books.

**File: `app/rag/embedder.py`**

`async def embed_query(text: str, tenant_id: str) -> list[float]`

Checks Redis first using key `embed:{tenant_id}:{sha256(text)}`. If found, deserialise and return. If not, call the embedding server `POST /embed` with the text. Store result in Redis with 7-day TTL. Return the 1024-float vector.

**File: `app/rag/vector_search.py`**

`async def vector_search(vector: list[float], tenant_id: str, top_k: int = 20) -> list[dict]`

Query Qdrant collection `documents`. Filter: `tenant_id` must equal the passed `tenant_id`. Return top_k results. Each result dict must include: `text`, `book_name`, `author`, `page_start`, `chapter`, `bbox`, `minio_path`, `score`, `book_id`.

**File: `app/rag/keyword_search.py`**

`async def keyword_search(query: str, tenant_id: str, top_k: int = 20) -> list[dict]`

Query Meilisearch index `documents`. Filter: `tenant_id = "{tenant_id}"`. Return top_k results in the same dict format as vector_search (add a `score` field set to 0.5 for all keyword results as a default relevance).

**File: `app/rag/reranker.py`**

`def rerank(question: str, results: list[dict], top_k: int = 5) -> list[dict]`

Two steps:

Step 1 — Reciprocal Rank Fusion: merge the vector and keyword result lists. Score formula: `1 / (rank + 60)` for each result in each list. Sum scores for results that appear in both lists. Sort descending by combined score.

Step 2 — Cross-encoder: load `cross-encoder/ms-marco-MiniLM-L-6-v2` once at module level using `sentence_transformers.CrossEncoder`. Score the top 20 fused results as `(question, passage_text)` pairs. Sort by cross-encoder score descending. Return top `top_k`.

**File: `app/rag/cache.py`**

`async def get_cached_answer(tenant_id: str, question: str) -> dict | None`
`async def set_cached_answer(tenant_id: str, question: str, response: dict) -> None`

Key: `cache:{tenant_id}:{sha256(normalised_question)}`. Normalise the question by stripping whitespace and lowercasing before hashing. TTL: 24 hours. Value: JSON-serialised response dict.

---

## Acceptance criteria

- [ ] `embed_query` with an Arabic question returns a list of exactly 1024 floats
- [ ] `embed_query` called twice with the same text — second call returns in under 5ms (Redis cache hit)
- [ ] `vector_search` returns only chunks where `tenant_id` matches — verified by checking all returned results
- [ ] `keyword_search` with an exact Arabic word returns chunks containing that word
- [ ] `rerank` with combined results returns exactly 5 items (or fewer if total results are fewer than 5)
- [ ] Cross-encoder scores are different for different passage-question pairs (not all the same value)
- [ ] `get_cached_answer` returns `None` for a question that has never been asked
- [ ] `set_cached_answer` then `get_cached_answer` with the same question returns the stored response
- [ ] Running `vector_search` with a `tenant_id` that has no documents returns an empty list — not an error
