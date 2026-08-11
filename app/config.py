from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    DEFAULT_TENANT_ID: str = "al-mawsuat-deobandiyyah"

    DATABASE_URL: str
    QDRANT_HOST: str = "qdrant"
    QDRANT_PORT: int = 6333
    MEILISEARCH_URL: str = "http://meilisearch:7700"
    MEILISEARCH_KEY: str
    # How long to wait for a Meilisearch task (create index, settings updates,
    # add_documents) before giving up. The client default is 5000ms, which
    # trips spuriously when the index is busy processing a queue of tasks.
    MEILISEARCH_TASK_TIMEOUT_MS: int = 120_000
    # Poll interval while waiting for a Meilisearch task.
    MEILISEARCH_TASK_INTERVAL_MS: int = 500
    REDIS_URL: str = "redis://redis:6379/0"

    MINIO_ENDPOINT: str = "minio:9000"
    MINIO_ACCESS_KEY: str
    MINIO_SECRET_KEY: str
    MINIO_BUCKET_BOOKS: str = "books"
    MINIO_BUCKET_HIGHLIGHTS: str = "highlights"

    GEMINI_API_KEY: str = ""
    GEMINI_API_BASE: str = "https://generativelanguage.googleapis.com"
    GEMINI_EMBEDDING_MODEL: str = "gemini-embedding-001"
    GEMINI_OCR_MODEL: str = "gemini-3.1-flash-lite"
    GEMINI_TRANSLATE_MODEL: str = "gemini-3.1-flash-lite"

    OCR_ENGINE: str = "tesseract"
    OCR_DPI: int = 200
    OCR_MAX_WORKERS: int = 0
    OCR_RATE_LIMIT_RPM: int = 1000
    OCR_CONCURRENT_BATCHES: int = 5
    OCR_MAX_RETRIES: int = 10
    OCR_RETRY_MAX_DELAY: int = 120
    EMBED_RATE_LIMIT_RPM: int = 1000
    EMBED_CONCURRENT_BATCHES: int = 10

    GROQ_API_KEY: str = ""
    GROQ_BASE_URL: str = "https://api.groq.com/openai/v1"
    GROQ_LLM_MODEL: str = "llama-3.3-70b-versatile"

    DEEPSEEK_API_KEY: str = ""
    DEEPSEEK_BASE_URL: str = "https://api.deepseek.com"
    DEEPSEEK_LLM_MODEL: str = "deepseek-v4-flash"

    GEMINI_LLM_MODEL: str = "gemini-3.1-flash-lite"

    # Ordered list of LLM providers to activate at chain-build time (comma-
    # separated). Providers not listed are skipped even when their API key is
    # set — e.g. a dead DeepSeek key must not cost a retry cycle on every
    # generation call. Default: Groq then Gemini.
    LLM_PROVIDERS: str = "groq,gemini"

    # Minimum reranked match score of the top retrieved chunk before the
    # system generates an answer. Below this, the system refuses with a
    # no-result message instead of producing a vague answer from weak
    # fragments. Reranked scores blend vector RRF, term overlap (0-1) and
    # entity boost. The gate also accepts matches whose raw Qdrant cosine
    # clears RAG_VECTOR_MIN_CONFIDENCE (see below), which rescues long,
    # genuinely-answerable English questions whose lexical overlap is diluted
    # below this threshold. Observed corpus behaviour: relevant lexical
    # matches score ~0.36-0.52, Arabic-overlap false positives (e.g. a riba
    # question matching an unrelated Arabic chunk) reach ~0.27, so this must
    # stay above ~0.28 unless the vector gate is relied on.
    RAG_MIN_CONFIDENCE_SCORE: float = 0.30

    # Second confidence signal for the quality gate: the raw Qdrant cosine of
    # the top vector hit, measured before reranking. The reranked score is
    # dominated by lexical overlap, so a long answerable question ("Which two
    # Qur'anic verses...") can fall below RAG_MIN_CONFIDENCE_SCORE even when
    # its top vector hit is semantically close. Observed: relevant English
    # content scores 0.65-0.78, overlapping-but-irrelevant noise 0.44-0.57.
    # The gate generates when EITHER best_score >= RAG_MIN_CONFIDENCE_SCORE
    # OR top_vector_score >= this value.
    RAG_VECTOR_MIN_CONFIDENCE: float = 0.65

    # Relaxed vector bar for source/footnote/transmission questions. Such
    # questions' words barely overlap the answer chunk's text, so the rerank
    # score lands well below RAG_MIN_CONFIDENCE_SCORE even when the vector
    # search pinned the right chunk (~0.62 vs the general gate's 0.65).
    RAG_CITATION_VECTOR_MIN_CONFIDENCE: float = 0.60

    GOOGLE_TRANSLATE_API_KEY: str = ""
    GOOGLE_TRANSLATE_BASE_URL: str = "https://translation.googleapis.com/language/translate/v2"

    CORS_ORIGINS: list[str] = ["http://localhost:3000", "http://localhost:3001"]
    CORS_ORIGIN_REGEX: str = r"http://localhost:\d+"

    JWT_SECRET_KEY: str
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    SUPERADMIN_EMAIL: str = "superadmin@al-mawsuat.local"
    SUPERADMIN_PASSWORD: str = ""

    ENVIRONMENT: str = "development"
    LOG_LEVEL: str = "INFO"
    SENTRY_DSN: str = ""


settings = Settings()
