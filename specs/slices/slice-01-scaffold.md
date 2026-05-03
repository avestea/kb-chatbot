# Slice 1 — FastAPI Scaffold & Database Schema

**Depends on:** Slice 0 (Docker stack running)
**Prereq:** Read `00-prompt-prefix.md` first.

---

## Goal

Runnable FastAPI server with PostgreSQL schema, Alembic migrations, and environment wiring. No business logic yet. The server starts inside the `api` container; Postgres, Redis, and MinIO are reached over the compose network.

## Deliverables

- `api/pyproject.toml` with pinned dependencies.
- `api/src/main.py` — FastAPI app with lifespan, `/health` route, global error handler.
- `api/src/config/env.py` — pydantic-settings validated environment.
- `api/src/db/base.py` — SQLAlchemy async engine + session factory.
- `api/src/db/models.py` — all ORM table definitions.
- `api/alembic.ini` + `api/alembic/env.py` — async Alembic setup.
- `api/alembic/versions/0001_init.py` — initial migration with all tables + HNSW index.
- `api/src/lib/errors.py` — typed `ApiError` subclasses.
- `api/src/lib/log.py` — structlog configuration.
- `api/src/lib/s3.py` — aioboto3 session singleton.
- `api/src/lib/redis.py` — redis-py async connection singleton.

## `pyproject.toml`

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "kbchat-api"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "fastapi[standard]>=0.115.0",
    "uvicorn[standard]>=0.32.0",
    "sqlalchemy[asyncio]>=2.0.36",
    "asyncpg>=0.30.0",
    "alembic>=1.14.0",
    "pgvector>=0.3.6",
    "pydantic>=2.10.0",
    "pydantic-settings>=2.6.0",
    "redis[asyncio]>=5.2.0",
    "arq>=0.26.0",
    "aioboto3>=13.2.0",
    "openai>=1.58.0",
    "anthropic>=0.40.0",
    "svix>=1.45.0",
    "tiktoken>=0.8.0",
    "pdfplumber>=0.11.0",
    "python-docx>=1.1.0",
    "beautifulsoup4>=4.12.0",
    "lxml>=5.3.0",
    "httpx>=0.28.0",
    "python-multipart>=0.0.18",
    "structlog>=24.4.0",
    "prometheus-client>=0.21.0",
    "tenacity>=9.0.0",
]

[dependency-groups]
dev = [
    "pytest>=8.3.0",
    "pytest-asyncio>=0.24.0",
    "pytest-cov>=6.0.0",
]

[tool.hatch.build.targets.wheel]
packages = ["src"]
```

## Environment config (`api/src/config/env.py`)

```python
from pydantic_settings import BaseSettings
from pydantic import Field

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
    CLERK_SECRET_KEY: str
    CLERK_WEBHOOK_SECRET: str

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
```

## SQLAlchemy ORM models (`api/src/db/models.py`)

```python
import uuid
from datetime import datetime
from typing import Optional
from sqlalchemy import (
    String, Text, Integer, Boolean, DateTime, Index,
    UniqueConstraint, ForeignKey, func
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID, JSONB
from pgvector.sqlalchemy import Vector

class Base(DeclarativeBase):
    pass

class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    plan: Mapped[str] = mapped_column(String(20), nullable=False, default='free')
    clerk_user_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    stripe_customer_id: Mapped[Optional[str]] = mapped_column(Text)
    stripe_subscription_id: Mapped[Optional[str]] = mapped_column(Text)
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    chatbots: Mapped[list["Chatbot"]] = relationship(back_populates="tenant")

class Chatbot(Base):
    __tablename__ = "chatbots"
    __table_args__ = (Index("chatbots_tenant_idx", "tenant_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    system_prompt_override: Mapped[Optional[str]] = mapped_column(Text)
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    tenant: Mapped["Tenant"] = relationship(back_populates="chatbots")
    documents: Mapped[list["Document"]] = relationship(back_populates="chatbot")

class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (Index("documents_chatbot_idx", "chatbot_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    chatbot_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("chatbots.id"), nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False)
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    mime_type: Mapped[str] = mapped_column(Text, nullable=False)
    s3_key: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default='pending')
    page_count: Mapped[Optional[int]] = mapped_column(Integer)
    error_reason: Mapped[Optional[str]] = mapped_column(Text)
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    chatbot: Mapped["Chatbot"] = relationship(back_populates="documents")
    chunks: Mapped[list["Chunk"]] = relationship(back_populates="document")

class Chunk(Base):
    __tablename__ = "chunks"
    __table_args__ = (
        Index("chunks_chatbot_idx", "chatbot_id"),
        # HNSW index added manually in migration — see below
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("documents.id"), nullable=False)
    chatbot_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("chatbots.id"), nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(1536), nullable=False)
    embedding_model: Mapped[str] = mapped_column(Text, nullable=False)
    document: Mapped["Document"] = relationship(back_populates="chunks")

class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (
        UniqueConstraint("chatbot_id", "session_id", name="conversations_chatbot_session_uq"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    chatbot_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("chatbots.id"), nullable=False)
    session_id: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    messages: Mapped[list["Message"]] = relationship(back_populates="conversation")

class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (Index("messages_conversation_idx", "conversation_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(10), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    source_chunk_ids: Mapped[Optional[list[str]]] = mapped_column(JSONB)
    tokens_used: Mapped[Optional[int]] = mapped_column(Integer)
    no_answer: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    prompt_version: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    conversation: Mapped["Conversation"] = relationship(back_populates="messages")
```

## Async DB engine (`api/src/db/base.py`)

```python
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from typing import AsyncGenerator
from src.config.env import settings

engine = create_async_engine(
    settings.DATABASE_URL,
    pool_size=10,
    max_overflow=20,
    pool_timeout=30,
    pool_pre_ping=True,
)
async_session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with async_session() as session:
        yield session
```

## Alembic setup

Configure `alembic.ini` to point at the DB URL from env. `alembic/env.py` must use the async engine:

```python
# alembic/env.py (key parts)
import asyncio
from sqlalchemy.ext.asyncio import create_async_engine
from src.db.models import Base
from src.config.env import settings

def run_migrations_online():
    connectable = create_async_engine(settings.DATABASE_URL)

    async def do_run():
        async with connectable.connect() as connection:
            await connection.run_sync(context.run_migrations)

    asyncio.run(do_run())
```

**Generate migration:**
```bash
docker compose exec api alembic revision --autogenerate -m "init"
```

**Then hand-append to the generated file:**
```python
# At the end of the upgrade() function:
op.execute("""
    CREATE INDEX IF NOT EXISTS chunks_embedding_hnsw
    ON chunks USING hnsw (embedding vector_cosine_ops);
""")
op.execute("""
    CREATE INDEX IF NOT EXISTS chunks_chatbot_embedding_idx
    ON chunks (chatbot_id);
""")
```

**Apply:**
```bash
docker compose exec api alembic upgrade head
```

## FastAPI app (`api/src/main.py`)

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from src.lib.errors import ApiError
from src.lib.log import log
from src.db.base import engine

@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("api starting")
    yield
    log.info("api shutting down")
    await engine.dispose()

app = FastAPI(lifespan=lifespan, title="KBChat API", version="0.1.0")

@app.exception_handler(ApiError)
async def api_error_handler(request: Request, exc: ApiError):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message, "details": exc.details}},
    )

@app.exception_handler(Exception)
async def generic_error_handler(request: Request, exc: Exception):
    log.error("unhandled error", exc_info=exc)
    return JSONResponse(
        status_code=500,
        content={"error": {"code": "internal_error", "message": "An unexpected error occurred"}},
    )

@app.get("/health")
async def health():
    # Slice 1 stub — DB/Redis checks added here
    return {"status": "ok"}
```

## Error classes (`api/src/lib/errors.py`)

```python
from typing import Any

class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str, details: Any = None):
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details

class BadRequestError(ApiError):
    def __init__(self, message="Bad request", details=None):
        super().__init__(400, "bad_request", message, details)

class UnauthenticatedError(ApiError):
    def __init__(self, message="Unauthorized"):
        super().__init__(401, "unauthenticated", message)

class ForbiddenError(ApiError):
    def __init__(self, message="Forbidden"):
        super().__init__(403, "forbidden", message)

class NotFoundError(ApiError):
    def __init__(self, message="Not found"):
        super().__init__(404, "not_found", message)

class ConflictError(ApiError):
    def __init__(self, message="Conflict"):
        super().__init__(409, "conflict", message)

class PayloadTooLargeError(ApiError):
    def __init__(self, message="Payload too large"):
        super().__init__(413, "payload_too_large", message)

class UnsupportedMimeTypeError(ApiError):
    def __init__(self, mime_type: str = ""):
        super().__init__(415, "unsupported_media_type", f"Unsupported file type: {mime_type}")

class ValidationError(ApiError):
    def __init__(self, details=None):
        super().__init__(422, "validation_failed", "Validation failed", details)

class RateLimitError(ApiError):
    def __init__(self, message="Rate limit exceeded"):
        super().__init__(429, "rate_limited", message)
```

## Health endpoint with real checks

```python
@app.get("/health")
async def health():
    from src.db.base import engine
    from src.lib.redis import redis_client
    import asyncio

    async def check_db():
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return "connected"

    async def check_redis():
        await redis_client.ping()
        return "connected"

    results = await asyncio.gather(
        check_db(), check_redis(), return_exceptions=True
    )
    db_ok = not isinstance(results[0], Exception)
    redis_ok = not isinstance(results[1], Exception)
    status = "ok" if (db_ok and redis_ok) else "degraded"
    code = 200 if status == "ok" else 503
    return JSONResponse(status_code=code, content={
        "status": status,
        "db": "connected" if db_ok else "error",
        "redis": "connected" if redis_ok else "error",
    })
```

## Acceptance criteria

- `docker compose up -d` → api container healthy.
- `curl localhost:8000/health` returns `200 {"status":"ok","db":"connected","redis":"connected"}`.
- Stopping postgres → `/health` returns `503` with `db: "error"`.
- `docker compose exec api alembic upgrade head` runs cleanly; all tables exist in Postgres.
- `docker compose exec postgres psql -U postgres -c "\dt"` shows: tenants, chatbots, documents, chunks, conversations, messages.
