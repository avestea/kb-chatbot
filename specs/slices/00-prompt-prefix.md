# Implementer Context — Prepend to Every Slice Prompt

> **How to use:** Paste this entire file before any individual slice file. Each slice assumes this context is loaded.

---

## Implementer Instructions

You are implementing a SaaS knowledge base chatbot builder in **Python**.
Tech stack: Python 3.12, FastAPI, SQLAlchemy 2.0 async, Alembic, PostgreSQL + pgvector,
ARQ (Redis queue), MinIO (local S3), Clerk for auth, Gradio for UI. **Everything runs
in Docker** — Postgres+pgvector, Redis, MinIO, api, worker, ui services are all in
`docker-compose.yml`. The developer's only host dependency is Docker.

Implement only the deliverables listed for the slice provided. Do not invent abstractions
not described here. Prefer explicit code over clever abstractions. All DB queries
must scope to `tenant_id`. Return code in full — no placeholders or TODOs unless explicitly noted.

---

## Tech Stack

| Layer | Choice |
|---|---|
| Backend | Python 3.12 + FastAPI + Uvicorn |
| Database | PostgreSQL 16 + pgvector |
| ORM | SQLAlchemy 2.0 async (`asyncpg` driver) |
| Migrations | Alembic |
| Validation | Pydantic v2 |
| Config | pydantic-settings |
| Queue | ARQ + Redis |
| Storage | MinIO (dev) / S3 (prod) via aioboto3 |
| Auth | Demo-first (`AUTH_MODE=demo` — any token, auto-create tenant). Swap to `AUTH_MODE=clerk` + Clerk JWT for production. |
| LLM | Anthropic `claude-sonnet-4-6` |
| Embeddings | OpenAI `text-embedding-3-small` |
| PDF parsing | pdfplumber |
| DOCX parsing | python-docx |
| HTML parsing | BeautifulSoup4 + lxml |
| Token counting | tiktoken |
| HTTP client | httpx (async) |
| Retry logic | tenacity |
| Logging | structlog |
| Metrics | prometheus-client |
| UI | Gradio |
| Testing | pytest + pytest-asyncio + httpx |

---

## Data Model

> Conceptual reference. Canonical SQLAlchemy ORM lives in `api/src/db/models.py`.
> Pydantic response schemas live in `api/src/schemas/`.

```python
# Pydantic response types — import these into every slice

from pydantic import BaseModel
from typing import Literal
from uuid import UUID
from datetime import datetime

class Tenant(BaseModel):
    id: UUID
    name: str
    plan: Literal['free', 'starter', 'pro', 'business']
    clerk_user_id: str
    deleted_at: datetime | None
    created_at: datetime
    model_config = {"from_attributes": True}

class Chatbot(BaseModel):
    id: UUID
    tenant_id: UUID
    name: str
    system_prompt_override: str | None
    deleted_at: datetime | None
    created_at: datetime
    model_config = {"from_attributes": True}

class Document(BaseModel):
    id: UUID
    chatbot_id: UUID
    tenant_id: UUID
    filename: str
    mime_type: str
    s3_key: str
    status: Literal['pending', 'processing', 'ready', 'error']
    page_count: int | None
    error_reason: str | None
    deleted_at: datetime | None
    created_at: datetime
    model_config = {"from_attributes": True}

class Conversation(BaseModel):
    id: UUID
    chatbot_id: UUID
    session_id: str
    created_at: datetime
    model_config = {"from_attributes": True}

class Message(BaseModel):
    id: UUID
    conversation_id: UUID
    role: Literal['user', 'assistant']
    content: str
    source_chunk_ids: list[str] | None
    tokens_used: int | None
    no_answer: bool
    prompt_version: str | None
    created_at: datetime
    model_config = {"from_attributes": True}
```

**Soft delete semantics:** `deleted_at IS NOT NULL` means logically deleted. Every read query against a soft-deletable table must include `WHERE deleted_at IS NULL`. Cascading is application-level: deleting a chatbot soft-deletes its documents in the same transaction.

---

## API Versioning

All `/api/...` routes are mounted under **`/api/v1/`**. Webhooks (`/webhooks/clerk`) and Gradio (`/ui`) stay unversioned.

## CORS Strategy

```python
# Three policies:
# /api/v1/chat/:id/message  → allow all origins (public — embedded chatbots)
# /api/v1/* (other)          → dashboard origin only (NEXT_PUBLIC_DASHBOARD_ORIGIN)
# /webhooks/*                → no CORS (server-to-server)
```

Configure with FastAPI `CORSMiddleware` per route group via sub-applications.

## Pagination

All list endpoints use this convention:

```python
# Query params: ?limit=25&offset=0
# Response:
class PaginatedResponse(BaseModel, Generic[T]):
    items: list[T]
    total: int
    has_more: bool

# limit defaults 25, max 100 (422 on overflow)
# offset defaults 0
# Always include ORDER BY created_at, id for stable pagination
```

## Error Conventions

All JSON routes return this shape on error:

```python
class ApiErrorDetail(BaseModel):
    code: str           # snake_case, stable
    message: str        # safe to surface to users
    details: Any | None = None

class ApiErrorResponse(BaseModel):
    error: ApiErrorDetail
```

| Status | code | When |
|---|---|---|
| 400 | `bad_request` | Malformed input |
| 401 | `unauthenticated` | Missing/invalid auth token |
| 403 | `forbidden` | Auth ok but not permitted |
| 404 | `not_found` | Doesn't exist or belongs to another tenant |
| 409 | `conflict` | Duplicate resource |
| 413 | `payload_too_large` | File over upload limit |
| 415 | `unsupported_media_type` | MIME type not in allow-list |
| 422 | `validation_failed` | Pydantic parse error; `details` has field errors |
| 429 | `rate_limited` | Limit hit; include `Retry-After` header |
| 500 | `internal_error` | Anything uncaught |

Implementation: custom exception classes + a FastAPI exception handler registered in `main.py`.

---

## Canonical File Layout

```
api/
  pyproject.toml           # dependencies + build config
  alembic.ini              # Alembic config
  Dockerfile
  alembic/
    env.py                 # async migration env
    versions/              # generated migration files
  src/
    main.py                # FastAPI app + lifespan          (Slice 1)
    worker.py              # ARQ WorkerSettings entrypoint   (Slice 5)
    config/
      env.py               # pydantic-settings               (Slice 1)
      chat.py              # chat tunables (N, budget)        (Slice 8)
    db/
      base.py              # async engine + session factory   (Slice 1)
      models.py            # all SQLAlchemy ORM models        (Slice 1)
      tenant_scope.py      # tenant_where() helper            (Slice 2)
    schemas/               # Pydantic request/response models
      chatbots.py                                             (Slice 3)
      documents.py                                            (Slice 4)
      chat.py                                                 (Slice 8)
    lib/
      log.py               # structlog setup                  (Slice 1)
      s3.py                # aioboto3 singleton               (Slice 1)
      redis.py             # redis-py async singleton         (Slice 1)
      embedder.py          # OpenAI embeddings                (Slice 6)
      llm.py               # Anthropic streaming wrapper      (Slice 8)
      errors.py            # ApiError subclasses              (Slice 1)
    auth/
      clerk.py             # JWT verification + tenant cache  (Slice 2)
      authenticate.py      # FastAPI dependency               (Slice 2)
      webhook.py           # POST /webhooks/clerk             (Slice 2)
    routes/
      health.py                                               (Slice 1)
      chatbots.py                                             (Slice 3)
      documents.py                                            (Slice 4)
      chat.py              # POST /api/v1/chat/:id/message    (Slice 8)
    rag/
      retrieve.py          # retrieve_context()               (Slice 7)
      prompt.py            # system prompt assembler          (Slice 8)
    worker/
      jobs.py              # job function definitions         (Slice 5)
      ingest_document.py   # main ingest handler              (Slice 5+6)
      parsers/
        __init__.py        # get_parser_for() dispatch        (Slice 5)
        pdf.py, docx.py, html.py, txt.py                      (Slice 5)
      chunker.py           # chunk_text()                     (Slice 6)
  tests/
    conftest.py            # fixtures, test DB, app           (Slice 1)
    factories.py           # model factories                  (Slice 1)
    fakes/
      openai.py            # fake embedder                    (Slice 6)
      anthropic.py         # scriptable fake LLM              (Slice 8)

web/
  requirements.txt         # gradio, httpx
  app.py                   # Gradio UI entrypoint             (Slice 9)
  Dockerfile

infra/
  postgres/init.sql        # CREATE EXTENSION vector
  minio/init.sh            # create bucket
  caddy/Caddyfile          # prod reverse proxy (Slice 10+)
```

---

## Cross-Slice Contracts (locked signatures)

Implement these EXACTLY. Later slices import them by path.

```python
# ---------------------------------------------------------------
# api/src/db/tenant_scope.py                        (Slice 2)
# ---------------------------------------------------------------
from sqlalchemy import and_, ColumnElement

def tenant_where(model, tenant_id: str, additional: ColumnElement | None = None) -> ColumnElement:
    """
    Always: model.tenant_id == tenant_id AND model.deleted_at IS NULL AND additional?
    EVERY tenant-scoped query must use this. Never write raw model.tenant_id == ... directly.
    """

# ---------------------------------------------------------------
# api/src/lib/embedder.py                           (Slice 6)
# ---------------------------------------------------------------
EMBEDDING_MODEL = 'text-embedding-3-small'
EmbeddingVector = list[float]  # length 1536

async def embed_chunks(contents: list[str]) -> list[EmbeddingVector]:
    """
    Batches up to 100 per OpenAI call.
    Retries 429/5xx with exponential backoff via tenacity (1s, 2s, 4s, max 3 attempts).
    Output order matches input order. Raises on permanent failure.
    Used by Slice 6 (ingest) and Slice 7 (retrieval — pass [query], take [0]).
    """

# ---------------------------------------------------------------
# api/src/worker/parsers/__init__.py                (Slice 5)
# ---------------------------------------------------------------
from typing import Callable, Awaitable

Parser = Callable[[bytes, str], Awaitable[str]]

def get_parser_for(mime_type: str) -> Parser:
    """Raises UnsupportedMimeTypeError on unknown types."""

# ---------------------------------------------------------------
# api/src/worker/chunker.py                         (Slice 6)
# ---------------------------------------------------------------
from dataclasses import dataclass

@dataclass
class DraftChunk:
    content: str
    token_count: int
    chunk_index: int

def chunk_text(text: str) -> list[DraftChunk]:
    """Pure function. No I/O. No randomness. Same input → same output."""

# ---------------------------------------------------------------
# api/src/rag/retrieve.py                           (Slice 7)
# ---------------------------------------------------------------
from dataclasses import dataclass

@dataclass
class RetrievedChunk:
    id: str
    document_id: str
    document_name: str
    content: str
    similarity: float  # cosine similarity in [0, 1]

async def retrieve_context(
    *,
    chatbot_id: str,
    query: str,
    top_k: int = 5,
    min_similarity: float = 0.75,
) -> list[RetrievedChunk]:
    """Returns [] when nothing passes the threshold."""

# ---------------------------------------------------------------
# api/src/lib/llm.py                                (Slice 8)
# ---------------------------------------------------------------
from dataclasses import dataclass
from typing import AsyncIterator
import asyncio

@dataclass
class ChatTurn:
    role: str   # 'user' | 'assistant'
    content: str

@dataclass
class TokenEvent:
    type: str = 'token'
    text: str = ''

@dataclass
class UsageEvent:
    type: str = 'usage'
    input_tokens: int = 0
    output_tokens: int = 0

StreamEvent = TokenEvent | UsageEvent

async def stream_completion(
    *,
    system_prompt: str,
    messages: list[ChatTurn],
    max_tokens: int = 1024,
    temperature: float = 0.2,
    cancel: asyncio.Event | None = None,
) -> AsyncIterator[StreamEvent]:
    """
    Yields TokenEvent then one final UsageEvent.
    If cancel is set mid-stream: stop yielding, raise asyncio.CancelledError.
    Raises on API error.
    """

# ---------------------------------------------------------------
# SSE event payloads — POST /api/v1/chat/:id/message  (Slice 8)
# ---------------------------------------------------------------
# Event order: meta → token...token → done
# On failure: single error event
#
# event: meta   data: {"conversation_id": "<uuid>", "source_count": <n>}
# event: token  data: {"text": "<partial>"}
# event: done   data: {"message_id": "<uuid>"}
# event: error  data: {"message": "<human-readable>"}
```

---

## Implementation Order

```
Slice 0  (Docker)
   └── Slice 1  (scaffold + DB)
         └── Slice 2  (auth)
               └── Slice 3  (chatbot CRUD)
                     └── Slice 4  (document upload)
                           └── Slice 5  (parsing worker)
                                 └── Slice 6  (chunk + embed + persist)
                                       └── Slice 7  (retrieval)
                                             └── Slice 8  (chat endpoint)
                                                   └── Slice 9  (Gradio UI)
                                                         ├── Slice 10 (explainability)
                                                         │     └── Slice 11 (evaluation dashboard)
                                                         │           └── Slice 14 (feedback)
                                                         ├── Slice 12 (hybrid search)
                                                         ├── Slice 13 (query rewriting)
                                                         └── Slice 15 (observability)
```

**MVP is Slices 0–9.** All in Python. No Node.js required.

---

## Docker Commands

```bash
# All commands run inside containers:
docker compose exec api alembic upgrade head
docker compose exec api pytest

# Wrong (runs on host):
alembic upgrade head
pytest
```

The `api` container runs `uvicorn src.main:app --host 0.0.0.0 --port 8000 --reload` in dev.
The `worker` container runs `arq src.worker.WorkerSettings`.
The `ui` container runs `python app.py`.

**Auth in demo mode (`AUTH_MODE=demo`):** any Bearer token works. The token value is the user identity. Tenant auto-created on first use. Use `Authorization: Bearer alice` in curl, or type `alice` in the Gradio token field. No Clerk account needed. Switch to `AUTH_MODE=clerk` + real keys when going to production.
