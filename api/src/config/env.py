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

    # App
    DASHBOARD_ORIGIN: str = "http://localhost:7860"

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = Settings()
