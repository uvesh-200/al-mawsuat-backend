from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

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

    GEMINI_API_KEY: str = ""
    GEMINI_API_BASE: str = "https://generativelanguage.googleapis.com"
    GEMINI_EMBEDDING_MODEL: str = "gemini-embedding-001"
    GEMINI_OCR_MODEL: str = "gemini-3.1-flash-lite"

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
