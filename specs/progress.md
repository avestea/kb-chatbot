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
| 1 | FastAPI Scaffold + DB Schema | Not started | |
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

### API skeleton (stub — will be replaced/expanded by Slice 1)

```
api/Dockerfile              multi-stage: base → dev → prod
api/pyproject.toml          full dependency list for all slices pre-declared
api/src/__init__.py         empty
api/src/main.py             FastAPI app with one route: GET /health → {"status":"ok"}
api/src/worker.py           ARQ WorkerSettings with a noop function placeholder
```

### Web skeleton (stub — will be replaced by Slice 9)

```
web/Dockerfile
web/requirements.txt        gradio, httpx
web/app.py                  minimal Gradio block, serves on :7860
```

### Running state

`docker compose up -d` brings all 7 services up healthy. Verified:
- `curl localhost:8000/health` → `{"status":"ok"}`
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

### 3. `api/pyproject.toml` — build backend

**Spec:** Not specified.  
**Actual:** `build-backend = "setuptools.build_meta"`  
**Why:** `setuptools.backends.legacy:build` (an incorrect variant) causes `BackendUnavailable` during the Docker build. `setuptools.build_meta` is the correct identifier.

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

Implement **Slice 1 — FastAPI Scaffold + DB Schema**.

Prompt:
```
Read specs/slices/00-prompt-prefix.md then implement: Slice 1 — FastAPI Scaffold + DB Schema
(specs/slices/slice-01-scaffold.md)
```

Slice 1 will:
- Replace `api/src/main.py` stub with a real FastAPI app (lifespan, middleware, exception handlers, structlog, Prometheus)
- Create all SQLAlchemy ORM models in `api/src/db/models.py`
- Create `api/src/db/base.py` (async engine + session factory)
- Create `api/src/config/env.py` (pydantic-settings)
- Create `api/src/lib/` utilities (log, s3, redis, errors)
- Set up Alembic and generate the initial migration
- Create `api/tests/conftest.py` and `api/tests/factories.py`
