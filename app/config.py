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

    SUPERADMIN_EMAIL: str = "superadmin@al-mawsuat.local"
    SUPERADMIN_PASSWORD: str = ""

    ENVIRONMENT: str = "development"
    LOG_LEVEL: str = "INFO"
    SENTRY_DSN: str = ""


settings = Settings()

