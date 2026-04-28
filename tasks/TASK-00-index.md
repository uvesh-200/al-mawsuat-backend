# Al-Mawsu'at al-Deobandiyyah — Phase 1 MVP Task Index

**Product:** Al-Mawsu'at al-Deobandiyyah  
**Phase:** 1 — MVP  
**Repos:** `al-mawsuat-backend` · `al-mawsuat-frontend`  
**Stack:** FastAPI · LangGraph · vLLM · Celery · Qdrant · Meilisearch · PostgreSQL · Redis · MinIO · Next.js 14

---

## How to use these task files

Each file in this `tasks/` folder is one complete feature. Read the task file, implement it, verify every acceptance criterion is met, then move to the next task. Do not start a task until all its dependencies are complete.

Give the task file directly to an AI coding assistant as the prompt — the description and acceptance criteria are written to be self-contained and AI-readable.

---

## Task list

| Task | Feature | Repo | Week | Depends on |
|---|---|---|---|---|
| TASK-01 | Infrastructure setup (packages, Docker, Python, folder structure) | backend | 1 | — |
| TASK-02 | Docker Compose services (all 11 services running) | backend | 1 | TASK-01 |
| TASK-03 | App config and environment variables | backend | 1 | TASK-01 |
| TASK-04 | Database models and tables (PostgreSQL + Alembic) | backend | 1 | TASK-03 |
| TASK-05 | Authentication (JWT, login, refresh, logout, rate limit) | backend | 2 | TASK-04 |
| TASK-06 | MinIO storage client (upload, download, cache check) | backend | 2 | TASK-03 |
| TASK-07 | PDF text and bbox extraction (PyMuPDF + Tesseract OCR) | backend | 3 | TASK-06 |
| TASK-08 | Text chunking (ayah / hadith / masail / fallback) | backend | 3 | TASK-07 |
| TASK-09 | Embedding inference server (bge-m3, standalone FastAPI) | backend | 3 | TASK-01 |
| TASK-10 | Indexer (Qdrant + Meilisearch + PostgreSQL writes) | backend | 3 | TASK-08, TASK-09 |
| TASK-11 | Celery worker + book upload API | backend | 3 | TASK-07, TASK-08, TASK-09, TASK-10 |
| TASK-12 | RAG retrieval (vector search + keyword search + reranking + cache) | backend | 4 | TASK-10 |
| TASK-13 | Translation server (NLLB-200 + language detection) | backend | 4 | TASK-01 |
| TASK-14 | LangGraph agent + vLLM answer generation | backend | 5 | TASK-12, TASK-13 |
| TASK-15 | Ask API endpoint (JSON + SSE streaming) | backend | 5 | TASK-14, TASK-12 |
| TASK-16 | Source highlighting endpoint (PDF page renderer) | backend | 6 | TASK-06, TASK-15 |
| TASK-17 | Frontend project setup (Next.js, API client, SSE hook) | frontend | 7 | TASK-15 |
| TASK-18 | Chat interface components (input, answer, sources, viewer) | frontend | 7 | TASK-17 |
| TASK-19 | Admin panel (upload, jobs, books table, login gate) | frontend | 7 | TASK-17, TASK-11 |
| TASK-20 | Production hardening (Sentry, rate limits, health, SSL, PgBouncer) | backend | 8 | all |

---

## Dependency diagram (simplified)

```
TASK-01 (setup)
  └── TASK-02 (docker)
  └── TASK-03 (config)
        └── TASK-04 (database)
              └── TASK-05 (auth)
        └── TASK-06 (minio)
              └── TASK-07 (extract)
                    └── TASK-08 (chunk)
                          └── TASK-10 (indexer) ←── TASK-09 (embedding server)
                                └── TASK-11 (celery + upload API)
                                └── TASK-12 (retrieval)
                                      └── TASK-14 (agent + vllm) ←── TASK-13 (translation)
                                            └── TASK-15 (ask API)
                                                  └── TASK-16 (highlight)
                                                  └── TASK-17 (frontend setup)
                                                        └── TASK-18 (chat UI)
                                                        └── TASK-19 (admin panel)
  └── TASK-20 (hardening) — after all others
```

---

## Phase 1 definition of done

The MVP is complete when all 20 tasks are done AND:

- [ ] Upload a 300-page Arabic PDF — processes to "ready" status
- [ ] Ask "ما حكم الربا؟" in Arabic — cited answer returned in under 10 seconds
- [ ] Ask the same question again — response in under 1 second (cached)
- [ ] Click a source card — PDF page opens with yellow highlight on the correct passage
- [ ] Ask an off-topic question — returns "No relevant information found in the provided sources."
- [ ] Arabic/Urdu answers render right-to-left correctly
- [ ] Admin can upload, view, and delete books
- [ ] App is accessible via HTTPS at your domain
- [ ] Sentry is tracking errors
- [ ] Health endpoint returns 200 with all checks passing
