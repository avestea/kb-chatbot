# KB Chatbot

A SaaS knowledge base chatbot builder. Operators upload documents; end-users chat against them via a RAG pipeline. Everything runs in Docker — the only host dependency is Docker.

## Quick Start

```bash
cp .env.example .env
docker compose up -d
docker compose exec api alembic upgrade head
curl localhost:8000/health   # → {"status":"ok","db":"connected","redis":"connected"}
# Gradio UI: http://localhost:7860
# MinIO console: http://localhost:9001  (minioadmin / minioadmin)
```

## Stack

| Layer | Choice |
|---|---|
| Backend | Python 3.12 + FastAPI |
| Database | PostgreSQL 16 + pgvector |
| Queue | ARQ + Redis |
| Storage | MinIO (dev) / S3 (prod) |
| LLM | Anthropic Claude Sonnet 4.6 |
| Embeddings | OpenAI text-embedding-3-small |
| Auth | Demo mode (any Bearer token) / Clerk (prod) |
| UI | Gradio |

## IDE Setup

```bash
uv sync --all-packages --extra dev
```

Creates `.venv` at the repo root covering both `api` and `web`. Point your IDE at `.venv` as the Python interpreter.

## Development

```bash
# Tail logs
docker compose logs -f

# Open a shell in the api container
docker compose exec api bash

# Run migrations
docker compose exec api alembic upgrade head

# Run tests
docker compose exec api pytest

# Full rebuild (after dependency changes)
docker compose down -v --remove-orphans && docker compose build --no-cache && docker compose up -d
```

## Auth

Set `AUTH_MODE=demo` in `.env` (default). Any Bearer token works — `Authorization: Bearer alice` auto-creates a tenant named "alice".

For production: set `AUTH_MODE=clerk` and fill in `CLERK_SECRET_KEY` / `CLERK_WEBHOOK_SECRET`.

## Ports

| Service | Port |
|---|---|
| API | 8000 |
| Gradio UI | 7860 |
| MinIO API | 9000 |
| MinIO Console | 9001 |
| PostgreSQL | 5432 |
| Redis | 6379 |

## Project Structure

```
api/                  FastAPI app + ARQ worker
  src/main.py         App entrypoint
  src/worker.py       ARQ worker entrypoint
  pyproject.toml      Dependencies
web/                  Gradio UI
  app.py              UI entrypoint
infra/
  postgres/init.sql   Enables pgvector extension
  minio/init.sh       Creates kbchat-dev bucket
specs/
  kb-chatbot-architecture.md   Full architecture reference
  progress.md                  Implementation status + LLM handoff notes
  slices/                      Slice-by-slice implementation prompts
```

## Implementation Status

See `specs/progress.md` for current slice status and LLM handoff context.
