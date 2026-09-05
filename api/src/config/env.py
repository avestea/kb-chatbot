from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # DB
    DATABASE_URL: str  # postgresql+asyncpg://...

    # Redis
    REDIS_URL: str

    # S3
    S3_ENDPOINT: str | None = None  # set for MinIO, unset for AWS
    S3_BUCKET: str
    S3_REGION: str = "us-east-1"
    S3_ACCESS_KEY_ID: str
    S3_SECRET_ACCESS_KEY: str

    # APIs
    OPENAI_API_KEY: str
    ANTHROPIC_API_KEY: str

    # Auth
    AUTH_MODE: str = "demo"          # "demo" | "clerk"
    CLERK_SECRET_KEY: str = ""       # only needed when AUTH_MODE=clerk
    CLERK_WEBHOOK_SECRET: str = ""   # only needed when AUTH_MODE=clerk

    # Retrieval
    # Cosine floor for a vector hit to count. Corpus- and model-dependent:
    # measured on the slice-spec corpus with text-embedding-3-small, answerable
    # questions scored 0.343-0.613 at top-1 and unanswerable ones 0.169-0.284,
    # so anything in (0.284, 0.343) separates them. The previous hard-coded 0.4
    # sat above the low end of the answerable range and discarded correct hits.
    RETRIEVAL_MIN_SIMILARITY: float = 0.32

    # App
    DASHBOARD_ORIGIN: str = "http://localhost:7860"

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = Settings()
