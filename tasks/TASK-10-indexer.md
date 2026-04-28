# TASK-10 — Indexer (Qdrant + Meilisearch + PostgreSQL)

**Feature:** Store processed chunks in all three databases  
**Repo:** al-mawsuat-backend  
**Week:** 3  
**Depends on:** TASK-08, TASK-09

---

## Description

Write `app/pipeline/indexer.py`. This module receives chunks (from chunker) and their embedding vectors (from embedding server) and writes them to all three data stores in parallel. It also updates the book record in PostgreSQL after indexing is complete.

**Qdrant indexing** — `index_to_qdrant(chunks: list[dict], vectors: list[list[float]]) -> None`

Create or upsert points into the `documents` collection. Each point:
- `id`: generate a UUID for each chunk
- `vector`: the 1024-float embedding vector
- `payload`: all chunk fields — `tenant_id`, `book_id`, `book_name`, `author`, `language`, `book_type`, `chapter`, `page_start`, `page_end`, `text`, `bbox`, `minio_path`

The Qdrant collection must be created with `size=1024` and `distance=Distance.COSINE` if it does not already exist. Check before creating.

**Meilisearch indexing** — `index_to_meilisearch(chunks: list[dict]) -> None`

Add chunks to the `documents` index. Each document must have an `id` field (UUID string). Configure filterable attributes: `tenant_id`, `book_id`, `language`, `book_type`. Configure searchable attributes: `text`, `chapter`, `book_name`, `author`. Do this configuration once — check if already configured before setting.

**PostgreSQL update** — `update_book_status(book_id: str, chunk_count: int, db: AsyncSession) -> None`

Update the book record: set `total_chunks = chunk_count` and `status = "ready"`.

All three writes can happen concurrently using `asyncio.gather()`.

The embedder batch helper `app/pipeline/embedder_batch.py` must also be written in this task:

`async def embed_chunks(chunks: list[dict]) -> list[list[float]]` — calls `POST /embed` on the embedding server with chunk texts in batches of 50. Returns a list of vectors in the same order as the input chunks.

---

## Acceptance criteria

- [ ] After indexing, `qdrant.count("documents")` returns the correct number of points
- [ ] Every Qdrant point has `tenant_id` in its payload
- [ ] Searching Meilisearch for a word that appears in the book returns that chunk
- [ ] Book record in PostgreSQL has `status = "ready"` and correct `total_chunks`
- [ ] Running the indexer twice for the same book does not create duplicate points in Qdrant (upsert, not insert)
- [ ] `embed_chunks` correctly handles a list of 200 chunks in batches of 50
- [ ] Qdrant collection is created automatically if it does not exist
