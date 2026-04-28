# TASK-11 — Celery Worker and Book Upload API

**Feature:** Async book processing pipeline + upload endpoint  
**Repo:** al-mawsuat-backend  
**Week:** 3  
**Depends on:** TASK-07, TASK-08, TASK-09, TASK-10

---

## Description

Wire together the full book processing pipeline using Celery for background processing, and expose the upload endpoint in FastAPI. When an admin uploads a PDF, the API returns immediately with a job ID, and Celery processes the book in the background.

**File: `workers/celery_worker.py`**

Configure a Celery app using Redis as the broker (`settings.REDIS_URL`). Define one task: `process_book(book_id: str, minio_path: str, tenant_id: str)`.

The task must execute these steps in order, updating the job status in PostgreSQL at each step:

1. Update job: `status="extracting"`, `progress_pct=10`
2. Download PDF bytes from MinIO using the minio_client
3. Update job: `status="chunking"`, `progress_pct=30`
4. Run `extractor.extract(pdf_bytes)` to get pages
5. Run `chunker.chunk(pages, tenant_id)` to get chunks
6. Update job: `status="embedding"`, `progress_pct=60`
7. Run `embedder_batch.embed_chunks(chunks)` to get vectors
8. Update job: `status="indexing"`, `progress_pct=85`
9. Run `indexer.index_to_qdrant(chunks, vectors)`
10. Run `indexer.index_to_meilisearch(chunks)`
11. Run `indexer.update_book_status(book_id, len(chunks))`
12. Update job: `status="completed"`, `progress_pct=100`, `finished_at=now()`

If any step raises an exception, catch it, update job: `status="failed"`, `error_msg=str(exception)`, then re-raise.

**File: `app/api/books.py`**

Three endpoints — all require authentication (`Depends(get_current_user)`):

`POST /admin/books/upload` — accepts multipart form data: `title`, `author`, `language`, `book_type`, and `file` (the PDF). Steps: validate file is PDF, save to MinIO at `books/{tenant_id}/{book_id}/original.pdf`, create Book record in PostgreSQL (status=pending), create ProcessingJob record (status=queued), trigger Celery task with `.delay()`, return `{ "job_id": "...", "book_id": "...", "status": "queued" }`.

`GET /admin/books` — returns list of all books where `tenant_id = settings.DEFAULT_TENANT_ID`. Returns book id, title, author, language, status, total_chunks, created_at.

`DELETE /admin/books/{book_id}` — deletes the book's vectors from Qdrant (filter by book_id and tenant_id), deletes from Meilisearch, deletes original PDF from MinIO, deletes the Book record from PostgreSQL (cascades to ProcessingJob).

**File: `app/api/jobs.py`**

`GET /admin/jobs/{job_id}` — returns the ProcessingJob record: status, progress_pct, current_step, error_msg, started_at, finished_at.

Add `embedding_server` Dockerfile to docker-compose and ensure Celery worker runs as a separate service using `Dockerfile.worker` (same image as main app but CMD is `celery -A workers.celery_worker worker --loglevel=info`).

---

## Acceptance criteria

- [ ] `POST /admin/books/upload` with a real Arabic PDF returns `{ job_id, book_id, status: "queued" }` in under 2 seconds
- [ ] Polling `GET /admin/jobs/{job_id}` shows progress advancing: queued → extracting → chunking → embedding → indexing → completed
- [ ] After completion, `GET /admin/books` shows the book with `status: "ready"` and a non-zero `total_chunks`
- [ ] Chunks appear in Qdrant with correct `tenant_id` and `bbox` fields
- [ ] Chunks appear in Meilisearch and are searchable by Arabic text
- [ ] `DELETE /admin/books/{book_id}` removes all data — verified by checking Qdrant count decreases
- [ ] Uploading a non-PDF file returns HTTP 422
- [ ] If Celery task fails (e.g. corrupted PDF), job status becomes "failed" with a readable error_msg
