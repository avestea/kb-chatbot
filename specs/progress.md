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
| 2 | Auth & Multi-Tenancy | **Done** | Demo mode fully working; all 8 AC tests pass. See deviations below. |
| 3 | Chatbot CRUD | **Done** | Full CRUD; 18 tests pass. See deviations below. |
| 4 | Document Upload | **Done** | 18/18 tests pass. S3 + ARQ mocked in tests. See deviations below. |
| 5 | Parsing Worker | **Done** | 17/17 tests pass. Worker boots and registers `ingest_document`. See deviations below. |
| 6 | Chunk + Embed + Persist | **Done** | 78/78 tests pass (34 new). Chunks table populated; embeddings via OpenAI. See deviations below. |
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

### Chunk + Embed + Persist (Slice 6)

```
api/src/worker/chunker.py       chunk_text(): tiktoken cl100k_base; TARGET=400 tokens, OVERLAP=50, MIN=20; pure function
api/src/lib/embedder.py         embed_chunks(): AsyncOpenAI, batches 100/call, tenacity retry (3×, exp backoff 1–4s)
api/tests/fakes/__init__.py     empty package marker
api/tests/fakes/openai.py       fake_embed_chunks(): returns [0.1]*1536 per item, no real API calls
api/src/worker/jobs.py          Extended: parse → chunk → idempotency-delete → embed+insert (batch 100) → ready
api/tests/test_worker.py        34 tests (was 17): chunker units, embedder units, chunk DB fields,
                                no-chunks error, embed-failure wipe, idempotency retry, status transitions
```

Verified ACs:
- Sufficient text → `chunks` table has rows with 1536-dim `embedding`, ascending `chunk_index`, `embedding_model = 'text-embedding-3-small'`
- `Document.status` transitions: `pending` → `processing` → `ready`
- `Document.error_reason` is null on success
- Short text (< 20 tokens) → `status = error`, `error_reason = "document produced no chunks"`
- Embed failure → `status = error`, `error_reason` set, partial chunks wiped in same transaction
- Retry after partial run: idempotency step deletes prior chunks, fresh chunks inserted cleanly
- All 78 tests pass (no regressions in Slices 1–5)

---

### Infrastructure (Slice 0)

```
docker-compose.yml          7 services: postgres, redis, minio, minio-init, api, worker, ui
infra/postgres/init.sql     CREATE EXTENSION IF NOT EXISTS vector
infra/minio/init.sh         creates kbchat-dev bucket on first boot
.env                        all local/dummy values — committed intentionally
.env.example                same keys, placeholder values
Makefile                    up / down / logs / psql / redis-cli / sh-api / migrate / rebuild
```

### Parsing Worker (Slice 5)

```
api/src/worker/__init__.py          WorkerSettings (functions=[ingest_document], max_jobs=1, job_timeout=600)
api/src/worker/jobs.py              ingest_document ARQ job: S3 download → parse → status transitions
api/src/worker/parsers/__init__.py  get_parser_for() MIME dispatch; raises UnsupportedMimeTypeError on unknown
api/src/worker/parsers/pdf.py       pdfplumber-based PDF → text
api/src/worker/parsers/docx.py      python-docx DOCX → text
api/src/worker/parsers/html.py      BeautifulSoup4 + lxml HTML → text (strips script/style/nav/footer/header)
api/src/worker/parsers/txt.py       UTF-8 decode (errors=replace)
api/src/db/base.py                  Added: async_session = async_session_factory alias
api/tests/test_worker.py            17 tests: parser units, dispatch, job integration (real DB + mocked S3)
```

Verified ACs:
- Worker boots via `docker compose up worker`: logs `Starting worker for 1 functions: ingest_document`
- TXT/HTML documents: status transitions from `pending` → `processing` after `ingest_document` runs
- Corrupt PDF: `Document.status` → `error` with `error_reason` populated; worker keeps running
- Unsupported MIME: status `error` set; no `Retry` raised (permanent failure)
- Parse error on tries 1–2: `Retry` raised (deferred 5s / 25s); try 3: `error` set, no retry
- Missing document: returns silently, no crash

---

### Document Upload (Slice 4)

```
api/src/lib/s3.py               Updated: module-level Session singleton; exports s3_client() per cross-slice contract
api/src/lib/redis.py            Updated: added get_arq_pool() ARQ connection pool singleton
api/src/schemas/documents.py    DocumentResponse, DocumentListResponse
api/src/routes/documents.py     POST/GET/DELETE under /api/v1/chatbots/{chatbot_id}/documents
api/src/main.py                 Registered documents_router under /api/v1
api/tests/test_documents.py     18 tests covering all ACs
```

Verified ACs:
- `POST /api/v1/chatbots/:id/documents` with PDF → 201 `{"document": {..., "status": "pending"}}`, S3 put called, ARQ job enqueued
- File > 20 MB → 413 `payload_too_large`
- Unsupported MIME (e.g. `image/png`) → 415 `unsupported_media_type`
- `GET /api/v1/chatbots/:id/documents` → `{"items":[...],"total":N,"has_more":bool}`
- `GET` with `limit=200` → 422
- `DELETE /:chatbot_id/documents/:doc_id` → 204, S3 delete called, chunks hard-deleted, document soft-deleted
- Upload/list/delete against another tenant's chatbot → 404
- No `Authorization` header → 401

---

### Chatbot CRUD (Slice 3)

```
api/src/schemas/__init__.py     empty package marker
api/src/schemas/chatbots.py     CreateChatbotRequest, UpdateChatbotRequest, ChatbotResponse, ChatbotListResponse
api/src/routes/chatbots.py      Full CRUD: POST/GET/GET-by-id/PUT/DELETE under /api/v1/chatbots
api/tests/test_chatbots.py      18 tests covering all ACs
```

Verified ACs:
- `POST /api/v1/chatbots` → 201 `{"chatbot": {...}}`
- `GET /api/v1/chatbots` → `{"items":[...],"total":N,"has_more":bool}`
- `GET /api/v1/chatbots?limit=200` → 422
- Tenant A cannot GET/PUT/DELETE Tenant B's chatbot → 404
- DELETE soft-deletes chatbot; chatbot disappears from GET list; GET-by-id returns 404
- `PUT` with only `{"name": "New Name"}` updates only name; `system_prompt_override` unchanged

---

### Auth (Slice 2)

```
api/src/lib/cache.py            SimpleCache — generic TTL in-memory LRU cache
api/src/db/tenant_scope.py      tenant_where() — locked cross-slice SQL helper
api/src/auth/__init__.py        empty package marker
api/src/auth/clerk.py           verify_token() + get_or_create_tenant(); demo + clerk modes
api/src/auth/authenticate.py    get_current_tenant() FastAPI Depends; AuthenticatedTenant dataclass
api/src/auth/webhook.py         POST /webhooks/clerk — acks in demo mode, svix-verified in clerk mode
api/src/routes/__init__.py      empty package marker
api/src/routes/chatbots.py      GET /api/v1/chatbots stub (minimal list; full CRUD comes in Slice 3)
api/tests/test_auth.py          8 tests covering: 401 on no token, auto-create, isolation, idempotency,
                                empty-token, clerk-mode invalid JWT, cross-tenant scope, webhook ACK
```

Verified:
- `curl localhost:8000/api/v1/chatbots -H "Authorization: Bearer alice"` → 200 + starter chatbot
- `curl localhost:8000/api/v1/chatbots -H "Authorization: Bearer bob"` → different tenant_id
- `curl localhost:8000/api/v1/chatbots` (no header) → 401
- `docker compose exec api python3 -m pytest tests/test_auth.py` → 8 passed

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

### 2. `api/src/worker/` package instead of `api/src/worker.py`

**Spec:** `api/src/worker.py` — ARQ `WorkerSettings` entrypoint (flat file).  
**Actual:** `api/src/worker/__init__.py` — `WorkerSettings` lives in the package `__init__`.  
**Why:** Slice 5 adds `api/src/worker/jobs.py` and `api/src/worker/parsers/` sub-package. Python resolves `src.worker` to the directory package when both `src/worker.py` and `src/worker/` exist, so the flat file is shadowed. Moving `WorkerSettings` into `src/worker/__init__.py` ensures `arq src.worker.WorkerSettings` resolves correctly. The original `src/worker.py` is deleted.

### 3. `api/pyproject.toml` — build backend is hatchling (Slice 1 supersedes Slice 0)

**Slice 0 used:** `build-backend = "setuptools.build_meta"` (setuptools worked around a BackendUnavailable error)  
**Slice 1 replaced with:** `build-backend = "hatchling.build"` per the Slice 1 spec.  
**Current state:** hatchling. The `mkdir -p src` line in `api/Dockerfile` is retained as a harmless no-op (hatchling doesn't need it, but it doesn't hurt either).

### 4. `api/src/lib/log.py` — `add_logger_name` omitted

**Spec:** structlog configured (no specific processor list mandated).  
**Actual:** `structlog.stdlib.add_logger_name` is NOT in the processor chain.  
**Why:** `add_logger_name` reads `logger.name` which only exists on stdlib `Logger` objects. With `PrintLoggerFactory`, it raises `AttributeError: 'PrintLogger' object has no attribute 'name'` and crashes the server on startup. Do not add it back unless switching to a stdlib logger factory.

### 5. `api/src/lib/s3.py` — module-level session + `s3_client()` function (Slice 4 updated)

**Original Slice 1:** `get_s3_client()` decorated with `@asynccontextmanager`, creating a new session each call.  
**Slice 4 replacement:** matches the spec exactly — `_session` is a module-level `aioboto3.Session` singleton; `s3_client()` is a plain function that returns `_session.client("s3", **kwargs)`, which aioboto3 natively makes an async context manager. Usage: `async with s3_client() as s3: ...`

### 7. `api/src/routes/chatbots.py` — stub created in Slice 2 for AC testing

**Spec:** `GET /api/v1/chatbots` is an AC for Slice 2 but the chatbot CRUD router is a Slice 3 deliverable.  
**Actual:** A minimal `GET /api/v1/chatbots` list endpoint was created in Slice 2 to satisfy the AC. It uses `tenant_where()` and `get_current_tenant`. Slice 3 will expand this file to full CRUD.

### 8. `api/pyproject.toml` — `[dependency-groups]` changed to `[project.optional-dependencies]`

**Spec:** `[dependency-groups]` (PEP 735) for dev dependencies.  
**Actual:** Changed to `[project.optional-dependencies]` so `pip install -e ".[dev]"` in the Dockerfile resolves pytest and other dev tools. PEP 735 dependency groups are not resolved by pip's extras syntax.

### 9. `pyproject.toml` — pytest asyncio loop scope set to session

**Spec:** Not specified.  
**Actual:** Added `asyncio_default_fixture_loop_scope = "session"` and `asyncio_default_test_loop_scope = "session"` to `[tool.pytest.ini_options]`. Required because the SQLAlchemy asyncpg connection pool binds to the first event loop it encounters; per-test (function-scoped) loops cause "Event loop is closed" errors on the pool's connection cleanup. All async tests must share one session-scoped event loop.

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

Implement **Slice 7 — Retrieval Function**.

Prompt:
```
Read specs/slices/00-prompt-prefix.md then implement: Slice 7 — Retrieval Function
(specs/slices/slice-07-retrieval.md)
```
