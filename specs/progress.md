# Implementation Progress

> **For LLMs picking this up fresh:** Read this file, then `specs/slices/00-prompt-prefix.md`, then the slice file you are about to implement. That is everything you need. Do not read the other slice files unless you need to understand a downstream contract.

---

## Project Summary

A SaaS knowledge base chatbot builder in Python. Operators upload documents (PDF, DOCX, HTML, TXT); the system chunks, embeds, and stores them in pgvector. End-users chat against those documents via a RAG pipeline. Everything runs in Docker. The only host dependency is Docker.

**Full architecture:** `specs/kb-chatbot-architecture.md`  
**Slice prompts:** `specs/slices/slice-NN-*.md`  
**Shared context (prepend to every slice):** `specs/slices/00-prompt-prefix.md`

---

## Slice Status

| Slice | Title | Status | Notes |
|---|---|---|---|
| 0 | Docker / Dev Environment | **Done** | All containers healthy. See deviations below. |
| 1 | FastAPI Scaffold + DB Schema | **Done** | `/health` returns `{"status":"ok","db":"connected","redis":"connected"}`; all 6 tables + HNSW index created via Alembic. |
| 2 | Auth & Multi-Tenancy | Not started | |
| 3 | Chatbot CRUD | Not started | |
| 4 | Document Upload | Not started | |
| 5 | Parsing Worker | Not started | |
| 6 | Chunk + Embed + Persist | Not started | |
| 7 | Retrieval Function | Not started | |
| 8 | Chat Endpoint | Not started | |
| 9 | Gradio UI | Not started | |
| 10 | Explainability | Not started | |
| 11 | Evaluation Dashboard | Not started | |
| 12 | Hybrid Search | Not started | |
| 13 | Query Rewriting | Not started | |
| 14 | Feedback | Not started | |

MVP = Slices 0–9.

---

## What Exists Right Now

### Infrastructure (Slice 0)

```
docker-compose.yml          7 services: postgres, redis, minio, minio-init, api, worker, ui
infra/postgres/init.sql     CREATE EXTENSION IF NOT EXISTS vector
infra/minio/init.sh         creates kbchat-dev bucket on first boot
.env                        all local/dummy values — committed intentionally
.env.example                same keys, placeholder values
Makefile                    up / down / logs / psql / redis-cli / sh-api / migrate / rebuild
```

### API (Slice 1)

```
api/Dockerfile              multi-stage: base → dev → prod
api/pyproject.toml          hatchling build backend; full dependency list for all slices
api/alembic.ini             points sqlalchemy.url at %(DATABASE_URL)s env var
api/alembic/env.py          async migration env (create_async_engine + asyncio.run)
api/alembic/versions/0001_init.py   all tables + FK constraints + HNSW index on chunks.embedding
api/src/__init__.py         empty
api/src/main.py             FastAPI app: lifespan, ApiError handler, generic 500 handler, /health
api/src/worker.py           ARQ WorkerSettings with a noop function placeholder
api/src/config/env.py       pydantic-settings: DATABASE_URL, REDIS_URL, S3_*, OPENAI/ANTHROPIC keys, AUTH_MODE
api/src/db/base.py          async engine + async_session_factory + get_db() dependency
api/src/db/models.py        Tenant, Chatbot, Document, Chunk, Conversation, Message ORM models
api/src/lib/log.py          structlog configured with PrintLoggerFactory; exports module-level `log`
api/src/lib/errors.py       ApiError + subclasses (400/401/403/404/409/413/415/422/429)
api/src/lib/s3.py           get_s3_client() asynccontextmanager wrapping aioboto3
api/src/lib/redis.py        get_redis_client() lazy singleton (redis.asyncio)
api/tests/conftest.py       event_loop + db_session (pytest_asyncio) + models fixtures
api/tests/factories/        TenantFactory, ChatbotFactory, DocumentFactory, MessageFactory
```

### Web skeleton (stub — will be replaced by Slice 9)

```
web/Dockerfile
web/requirements.txt        gradio, httpx
web/app.py                  minimal Gradio block, serves on :7860
```

### Running state

`docker compose up -d` brings all 7 services up healthy. Verified:
- `curl localhost:8000/health` → `{"status":"ok","db":"connected","redis":"connected"}`
- `docker compose exec api alembic upgrade head` creates all 6 tables + HNSW index cleanly
- `docker compose exec postgres psql -U postgres -c "\dt"` shows: tenants, chatbots, documents, chunks, conversations, messages
- `docker compose exec postgres psql -U postgres -c "\d chunks"` confirms `embedding vector(1536)` and `chunks_embedding_hnsw` HNSW index
- `docker compose exec postgres psql -U postgres -c "\dx"` lists `vector` extension (pgvector 0.8.2)
- MinIO bucket `kbchat-dev` exists (console at http://localhost:9001, user/pass: minioadmin)
- Volumes persist across `docker compose down && docker compose up -d`

---

## Deviations from Spec

These are places where the implemented code differs from what the slice spec literally says. Future LLMs must preserve them.

### 1. `api/Dockerfile` — added `mkdir -p src` before editable install

**Spec:** `COPY pyproject.toml . && RUN pip install --no-cache-dir -e ".[dev]"`  
**Actual:**
```dockerfile
COPY pyproject.toml .
RUN mkdir -p src && pip install --no-cache-dir -e ".[dev]"
```
**Why:** setuptools editable install needs the `src/` directory to exist at build time so it can register the package. Without it, the install fails. In dev the real source is bind-mounted over `/app` at runtime — `mkdir -p src` just creates an empty placeholder for the build step.

### 2. `api/src/worker.py` — noop function in WorkerSettings

**Spec:** Worker just needs to boot; real jobs come in Slice 5.  
**Actual:** `functions = [noop]` instead of `functions = []`  
**Why:** ARQ raises `RuntimeError: at least one function or cron_job must be registered` with an empty list. Replace `noop` with real job functions in Slice 5 — do not keep it.

### 3. `api/pyproject.toml` — build backend is hatchling (Slice 1 supersedes Slice 0)

**Slice 0 used:** `build-backend = "setuptools.build_meta"` (setuptools worked around a BackendUnavailable error)  
**Slice 1 replaced with:** `build-backend = "hatchling.build"` per the Slice 1 spec.  
**Current state:** hatchling. The `mkdir -p src` line in `api/Dockerfile` is retained as a harmless no-op (hatchling doesn't need it, but it doesn't hurt either).

### 4. `api/src/lib/log.py` — `add_logger_name` omitted

**Spec:** structlog configured (no specific processor list mandated).  
**Actual:** `structlog.stdlib.add_logger_name` is NOT in the processor chain.  
**Why:** `add_logger_name` reads `logger.name` which only exists on stdlib `Logger` objects. With `PrintLoggerFactory`, it raises `AttributeError: 'PrintLogger' object has no attribute 'name'` and crashes the server on startup. Do not add it back unless switching to a stdlib logger factory.

### 5. `api/src/lib/s3.py` — `get_s3_client()` is an async context manager

**Spec:** "aioboto3 session singleton".  
**Actual:** `get_s3_client()` is decorated with `@asynccontextmanager`.  
**Why:** aioboto3 clients must be used as async context managers — `await session.client(...)` returns an internal `_AsyncClientCreator`, not the actual client. All callers must use `async with get_s3_client() as client:`.

### 6. `api/tests/factories/` — package, not flat file

**Spec:** `api/tests/factories.py`  
**Actual:** `api/tests/factories/__init__.py`  
**Why:** implemented as a package directory. Imports work identically: `from tests.factories import TenantFactory`.

---

## How to Run

```bash
# First time
docker compose up -d

# Rebuild images after dependency changes
docker compose down -v --remove-orphans
docker compose build --no-cache
docker compose up -d

# Tail all logs
docker compose logs -f

# Run migrations (Slice 1+)
docker compose exec api alembic upgrade head

# Run tests (Slice 1+)
docker compose exec api pytest
```

Auth is in demo mode (`AUTH_MODE=demo`). Any Bearer token value works. `Authorization: Bearer alice` auto-creates a tenant named "alice" on first use.

---

## Next Step

Implement **Slice 2 — Auth & Multi-Tenancy**.

Prompt:
```
Read specs/slices/00-prompt-prefix.md then implement: Slice 2 — Auth & Multi-Tenancy
(specs/slices/slice-02-auth.md)
```
