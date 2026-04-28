# TASK-03 — App Config and Environment Variables

**Feature:** Centralised configuration  
**Repo:** al-mawsuat-backend  
**Week:** 1  
**Depends on:** TASK-01

---

## Description

Write `app/config.py` using Pydantic Settings. This is the single source of truth for all configuration values across the entire backend. Every other module imports `settings` from this file — nothing else reads `.env` directly.

The config must include these variables with their types and defaults:

```
DEFAULT_TENANT_ID: str = "al-mawsuat-deobandiyyah"

DATABASE_URL: str
QDRANT_HOST: str = "qdrant"
QDRANT_PORT: int = 6333
MEILISEARCH_URL: str = "http://meilisearch:7700"
MEILISEARCH_KEY: str
REDIS_URL: str = "redis://redis:6379/0"

MINIO_ENDPOINT: str = "minio:9000"
MINIO_ACCESS_KEY: str
MINIO_SECRET_KEY: str
MINIO_BUCKET_BOOKS: str = "books"
MINIO_BUCKET_HIGHLIGHTS: str = "highlights"

KAFKA_BOOTSTRAP_SERVERS: str = "kafka:9092"
KAFKA_TOPIC_BOOKS: str = "book-processing"
KAFKA_CONSUMER_GROUP: str = "mawsuat-workers"

EMBEDDING_SERVER_URL: str = "http://embedding_server:8001"
VLLM_BASE_URL: str = "http://vllm:8003/v1"
VLLM_MODEL: str = "mistralai/Mistral-7B-Instruct-v0.3"
NLLB_SERVER_URL: str = "http://translation_server:8002"

JWT_SECRET_KEY: str
JWT_ALGORITHM: str = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
REFRESH_TOKEN_EXPIRE_DAYS: int = 7

ENVIRONMENT: str = "development"
LOG_LEVEL: str = "INFO"
SENTRY_DSN: str = ""
```

The class must read from `.env` file using `model_config = SettingsConfigDict(env_file=".env")`.

Create a singleton instance at the bottom: `settings = Settings()`

All other modules must import like: `from app.config import settings`

Also write `.env.example` with all variable names listed, no real values, and a comment on each line explaining what it is.

---

## Acceptance criteria

- [ ] `from app.config import settings` works without error
- [ ] `settings.DEFAULT_TENANT_ID` returns `"al-mawsuat-deobandiyyah"`
- [ ] If a required variable (like `JWT_SECRET_KEY`) is missing from `.env`, the app raises a clear validation error on startup — not a silent failure
- [ ] `.env.example` contains every variable name with a descriptive comment
- [ ] No variable is hardcoded anywhere else in the codebase — all come from `settings`
