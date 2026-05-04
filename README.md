# KB Chatbot

A SaaS knowledge base chatbot builder. Operators upload documents; end-users chat against them via a RAG pipeline. Everything runs in Docker — the only host dependency is Docker.

## Quick Start

```bash
cp .env.example .env
docker compose up -d
docker compose exec api alembic upgrade head
curl localhost:8000/health   # → {"status":"ok","db":"connected","redis":"connected"}

# Auth is in demo mode — any Bearer token works:
curl localhost:8000/api/v1/chatbots -H "Authorization: Bearer alice"
# → 200 with a starter chatbot auto-created for tenant "alice"

# Chatbot CRUD (Slice 3):
curl -X POST localhost:8000/api/v1/chatbots \
  -H "Authorization: Bearer alice" \
  -H "Content-Type: application/json" \
  -d '{"name":"My Bot","system_prompt_override":"Be concise."}'
# → 201 {"chatbot": {"id": "...", "name": "My Bot", ...}}

# Document Upload (Slice 4):
BOT_ID="<id from above>"
curl -X POST "localhost:8000/api/v1/chatbots/$BOT_ID/documents" \
  -H "Authorization: Bearer alice" \
  -F "file=@/path/to/doc.pdf"
# → 201 {"document": {"id": "...", "status": "pending", ...}}

curl "localhost:8000/api/v1/chatbots/$BOT_ID/documents" \
  -H "Authorization: Bearer alice"
# → {"items": [...], "total": 1, "has_more": false}

# The worker automatically processes uploaded documents:
#   pending → processing → ready   (chunks embedded into pgvector)
# Poll the document list to check status. Once "ready", the document
# is searchable. On failure, status = "error" with error_reason set.

# The worker automatically processes uploaded documents:
# poll document list until status = "ready", then chat against it:
DOC_ID="<id from upload>"

# Chat Endpoint (Slice 8) — public SSE stream, no auth required:
curl -N -X POST "localhost:8000/api/v1/chat/$BOT_ID/message" \
  -H "Content-Type: application/json" \
  -d '{"message":"What is the refund policy?","session_id":"my-session-1"}'
# → event: meta   data: {"conversation_id":"...","source_count":2}
# → event: token  data: {"text":"Returns are"}
# → event: token  data: {"text":" accepted within 30 days."}
# → event: done   data: {"message_id":"..."}

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

For production:

1. Create a Clerk application at [clerk.com](https://clerk.com) and copy the Secret Key and Webhook Secret.
2. Set in `.env`:
   ```
   AUTH_MODE=clerk
   CLERK_SECRET_KEY=sk_live_...
   CLERK_WEBHOOK_SECRET=whsec_...
   ```
3. Implement `api/src/lib/clerk_jwks.py` — the stub exists but raises `NotImplementedError`. Fill in the JWKS fetch from `https://api.clerk.com/v1/jwks` and return the matching signing key for `jwt.decode`.
4. Point the Clerk dashboard webhook at `POST https://yourdomain.com/webhooks/clerk`.

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
api/                          FastAPI app + ARQ worker
  src/main.py                 App entrypoint
  src/worker/__init__.py      ARQ WorkerSettings entrypoint
  src/worker/jobs.py          ARQ job: ingest_document (parse → chunk → embed → persist)
  src/worker/parsers/         PDF / DOCX / HTML / TXT parsers
  src/worker/chunker.py       chunk_text() — token-aware sentence chunker (tiktoken)
  src/lib/embedder.py         embed_chunks() — OpenAI text-embedding-3-small, batched + retried
  src/rag/retrieve.py         retrieve_context() — pgvector HNSW cosine search + similarity filter
  src/rag/prompt.py           build_system_prompt() — injects retrieved chunks into SYSTEM_TEMPLATE
  src/lib/llm.py              stream_completion() — Anthropic streaming wrapper (TokenEvent/UsageEvent)
  src/routes/chat.py          POST /api/v1/chat/{chatbot_id}/message — public SSE endpoint
  pyproject.toml              Dependencies
web/                          Gradio UI
  app.py                      UI entrypoint
infra/
  postgres/init.sql           Enables pgvector extension
  minio/init.sh               Creates kbchat-dev bucket
specs/
  kb-chatbot-architecture.md  Full architecture reference
  progress.md                 Implementation status + LLM handoff notes
  slices/                     Slice-by-slice implementation prompts
```

## Implementation Status

See `specs/progress.md` for current slice status and LLM handoff context.
