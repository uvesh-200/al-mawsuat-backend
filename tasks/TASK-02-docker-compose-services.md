# TASK-02 — Docker Compose Services

**Feature:** Local services orchestration  
**Repo:** al-mawsuat-backend  
**Week:** 1  
**Depends on:** TASK-01

---

## Description

Write the `docker-compose.yml` file at the repo root that starts all backend services with a single `docker compose up` command. No application code is written in this task — only infrastructure services.

The following services must be defined:

**postgres** — PostgreSQL 16 Alpine. Database for books, users, jobs. Data persisted in a named volume `postgres_data`.

**redis** — Redis 7 Alpine. Used for Celery job queue, query cache, session store. Data persisted in `redis_data`.

**qdrant** — Qdrant latest. Vector database for semantic search. Data persisted in `qdrant_data`. Expose port 6333.

**meilisearch** — Meilisearch latest. Keyword search engine. Requires `MEILI_MASTER_KEY` environment variable. Data persisted in `meili_data`. Expose port 7700.

**minio** — MinIO latest. Object storage for original PDFs and highlight images. Expose port 9000 (API) and 9001 (console). Data persisted in `minio_data`.

**nginx** — Nginx Alpine. Reverse proxy in front of FastAPI. Config mounted from `./infra/nginx/nginx.conf`.

Write `infra/nginx/nginx.conf` with:
- Listen on port 80
- `server_name localhost`
- Proxy all requests to `http://fastapi:8000`
- Pass `Host` header through

All services must:
- Use named volumes (not bind mounts) for persistent data
- Read secrets from environment variables, never hardcoded
- Have `restart: unless-stopped` policy
- Be on a shared network named `mawsuat_network`

Write a basic health check endpoint in `app/main.py`:
```python
from fastapi import FastAPI
app = FastAPI(title="Al-Mawsuat Backend")

@app.get("/health")
async def health():
    return {"status": "ok"}
```

Write `Dockerfile` for the FastAPI app:
- Base image: `python:3.12-slim`
- Install system deps: `libpq-dev`, `libmupdf-dev`, `tesseract-ocr`, `tesseract-ocr-ara`, `tesseract-ocr-urd`
- Copy and install `requirements.txt`
- Copy app code
- CMD: `gunicorn app.main:app -k uvicorn.workers.UvicornWorker -w 4 --bind 0.0.0.0:8000`

---

## Acceptance criteria

- [ ] `docker compose up -d` starts all services with no errors
- [ ] `docker compose ps` shows all services as `running`
- [ ] `curl http://localhost/health` returns `{"status": "ok"}`
- [ ] `curl http://localhost:6333/collections` returns Qdrant response
- [ ] `curl http://localhost:7700/health` returns Meilisearch health
- [ ] MinIO console accessible at `http://localhost:9001`
- [ ] `docker compose down -v` stops and removes everything cleanly
- [ ] No hardcoded passwords in `docker-compose.yml` — all from `.env`
