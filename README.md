# KB Chatbot

A SaaS knowledge base chatbot builder. Operators upload documents (PDF, DOCX, HTML, TXT) and end-users chat against them via a RAG (Retrieval-Augmented Generation) pipeline. Everything runs in Docker — the only host dependency is Docker.

## Prerequisites

- [Docker](https://docs.docker.com/get-docker/) with Compose v2 (`docker compose` command)
- An [OpenAI API key](https://platform.openai.com/api-keys) (for embeddings)
- An [Anthropic API key](https://console.anthropic.com/) (for chat responses)

## Quick Start

```bash
# 1. Copy the environment template
cp .env.example .env

# 2. Add your API keys to .env
#    Edit these two lines:
#      OPENAI_API_KEY=sk-...
#      ANTHROPIC_API_KEY=sk-ant-...

# 3. Start all services
docker compose up -d

# 4. Run database migrations
docker compose exec api alembic upgrade head

# 5. Confirm everything is healthy
curl localhost:8000/health
# → {"status":"ok","db":"connected","redis":"connected"}

# 6. Open the UI
open http://localhost:7860
```

## Using the Gradio UI

Navigate to **http://localhost:7860** and enter `alice` (or any string) as the API token.

### Step 1 — Create a chatbot

Go to the **Chatbots** tab → fill in a name → click **Create**. The chatbot appears in the list.

### Step 2 — Upload documents

Go to the **Documents** tab → select your chatbot → upload a PDF, DOCX, HTML, or TXT file → click **Upload**.

The document starts in `pending` status. The background worker automatically downloads it, parses the text, splits it into chunks, and embeds each chunk with OpenAI. Click **Refresh Documents** until status shows `ready` (usually 5–30 seconds depending on file size).

### Step 3 — Chat

Go to the **Chat** tab → select your chatbot → type a question → press Enter.

The response streams token-by-token. After it completes:
- A **Sources** table shows which document chunks were retrieved, with similarity scores and text snippets.
- **👍 / 👎** buttons let you rate the answer quality.
- Follow-up questions are automatically rewritten into self-contained queries before retrieval (you see the original question; the rewrite is invisible).

### Step 4 — Evaluate

Go to the **Evaluation** tab → click **Refresh**.

The dashboard shows:
- Total conversations and messages
- No-answer rate (how often the bot said it couldn't help)
- Average retrieval similarity
- Satisfaction rate from thumbs up/down feedback

Toggle **Show failures only** to filter to conversations where retrieval found nothing. Paste a conversation ID and click **Inspect** to see the full message thread with per-message ratings.

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `DATABASE_URL` | yes | _(set in .env.example)_ | PostgreSQL connection string |
| `REDIS_URL` | yes | _(set in .env.example)_ | Redis connection string |
| `S3_ENDPOINT` | yes | `http://minio:9000` | Object storage endpoint (omit for AWS S3) |
| `S3_BUCKET` | yes | `kbchat-dev` | Bucket name |
| `S3_ACCESS_KEY_ID` | yes | `minioadmin` | MinIO / AWS access key |
| `S3_SECRET_ACCESS_KEY` | yes | `minioadmin` | MinIO / AWS secret key |
| `OPENAI_API_KEY` | yes | — | Used for `text-embedding-3-small` |
| `ANTHROPIC_API_KEY` | yes | — | Used for Claude Sonnet 4.6 (chat) and Haiku 4.5 (query rewriting) |
| `AUTH_MODE` | no | `demo` | `demo` (any Bearer token works) or `clerk` |
| `CLERK_SECRET_KEY` | if clerk | — | Clerk secret key |
| `CLERK_WEBHOOK_SECRET` | if clerk | — | Clerk webhook signing secret |
| `DASHBOARD_ORIGIN` | no | `http://localhost:7860` | Allowed CORS origin for the UI |

## Service Ports

| Service | Port | URL |
|---|---|---|
| FastAPI backend | 8000 | http://localhost:8000 |
| Gradio UI | 7860 | http://localhost:7860 |
| MinIO console | 9001 | http://localhost:9001 (minioadmin / minioadmin) |
| PostgreSQL | 5432 | — |
| Redis | 6379 | — |
| MinIO API | 9000 | — |

## API Quick Reference

All management endpoints require `Authorization: Bearer <token>`. In demo mode any token works. The chat endpoint is public.

```bash
# Health
curl localhost:8000/health

# Create a chatbot
curl -X POST localhost:8000/api/v1/chatbots \
  -H "Authorization: Bearer alice" \
  -H "Content-Type: application/json" \
  -d '{"name":"My Bot","system_prompt_override":"Be concise."}'
# → 201 {"chatbot": {"id": "<UUID>", "name": "My Bot", ...}}

# Upload a document
BOT_ID="<UUID from above>"
curl -X POST "localhost:8000/api/v1/chatbots/$BOT_ID/documents" \
  -H "Authorization: Bearer alice" \
  -F "file=@/path/to/doc.pdf"
# → 201 {"document": {"id": "<UUID>", "status": "pending", ...}}

# Poll until status = "ready"
curl "localhost:8000/api/v1/chatbots/$BOT_ID/documents" \
  -H "Authorization: Bearer alice"

# Chat (SSE stream, no auth required)
curl -N -X POST "localhost:8000/api/v1/chat/$BOT_ID/message" \
  -H "Content-Type: application/json" \
  -d '{"message":"What is the refund policy?","session_id":"session-1"}'
# Events:
#   meta    → {"conversation_id":"...","source_count":2,"retrieval_query":"..."}
#   sources → {"sources":[{"index":1,"document_name":"policy.pdf","similarity":0.92,...}]}
#   token   → {"text":"Returns are"}  (repeated)
#   done    → {"message_id":"..."}

# Rate a response (thumbs up = 1, thumbs down = -1)
MSG_ID="<message_id from done event>"
curl -X POST localhost:8000/api/v1/feedback \
  -H "Authorization: Bearer alice" \
  -H "Content-Type: application/json" \
  -d "{\"message_id\":\"$MSG_ID\",\"rating\":1}"
# → 201 {"message_id":"...","rating":1}

# Analytics summary
curl "localhost:8000/api/v1/analytics/summary" \
  -H "Authorization: Bearer alice"
# → {"total_conversations":5,"no_answer_rate":0.25,"satisfaction_rate":0.667,...}

# Conversations with failures
curl "localhost:8000/api/v1/analytics/conversations?no_answer_only=true" \
  -H "Authorization: Bearer alice"

# Full message thread for a conversation
curl "localhost:8000/api/v1/analytics/conversations/<conv_id>/messages" \
  -H "Authorization: Bearer alice"
```

## Development

```bash
# Tail all logs
docker compose logs -f

# Tail a specific service
docker compose logs -f api
docker compose logs -f worker

# Shell into the API container
docker compose exec api bash

# Run tests
docker compose exec api pytest

# Run tests with coverage
docker compose exec api pytest --cov=src

# Re-run migrations from scratch
docker compose exec api alembic downgrade base
docker compose exec api alembic upgrade head

# Full rebuild after changing dependencies
docker compose down -v --remove-orphans
docker compose build --no-cache
docker compose up -d
docker compose exec api alembic upgrade head
```

### Local IDE Setup (no Docker)

```bash
# Creates .venv at repo root covering both api/ and web/
uv sync --all-packages --extra dev
```

Point your IDE's Python interpreter at `.venv/bin/python`.

## Authentication

**Demo mode** (default): any `Authorization: Bearer <token>` value auto-creates a tenant keyed to that token string. Use `Bearer alice`, `Bearer bob`, etc. to simulate multiple tenants. No external service needed.

**Clerk mode**: for production multi-user deployments.

1. Create a [Clerk](https://clerk.com) application and copy the Secret Key and Webhook Secret.
2. Set in `.env`:
   ```
   AUTH_MODE=clerk
   CLERK_SECRET_KEY=sk_live_...
   CLERK_WEBHOOK_SECRET=whsec_...
   ```
3. Implement `api/src/lib/clerk_jwks.py` — the stub raises `NotImplementedError`. Fetch the JWKS from `https://api.clerk.com/v1/jwks` and return the signing key for `jwt.decode`.
4. Point the Clerk dashboard webhook at `POST https://yourdomain.com/webhooks/clerk`.

## Project Structure

```
api/                        FastAPI backend + ARQ background worker
  src/
    main.py                 App entrypoint, router registration
    config/env.py           Pydantic Settings (reads .env)
    auth/                   Bearer token validation, Clerk webhook handler
    db/                     SQLAlchemy models, async engine, tenant scoping
    lib/                    LLM wrapper, embedder, S3 client, Redis pool
    rag/
      retrieve.py           Hybrid BM25 + vector search with RRF merge
      rewrite.py            Query rewriting via Claude Haiku
      prompt.py             System prompt builder
    routes/                 HTTP route handlers (chatbots, documents, chat, analytics, feedback)
    schemas/                Pydantic request/response models
    worker/
      jobs.py               ARQ job: parse → chunk → embed → persist
      chunker.py            Token-aware text chunker (tiktoken)
      parsers/              PDF, DOCX, HTML, TXT text extractors
  alembic/                  Database migrations (4 versions)
  tests/                    154 tests with factories and fakes

web/                        Gradio UI (4 tabs)
  app.py                    Chatbots / Documents / Chat / Evaluation tabs
  api_client.py             Typed httpx wrapper around the REST API

infra/
  postgres/init.sql         Enables pgvector extension on startup
  minio/init.sh             Creates the kbchat-dev bucket on startup

specs/                      Architecture docs and implementation notes
docker-compose.yml          Orchestrates all 7 services
.env.example                Environment variable template
```

See [ARCHITECTURE.md](ARCHITECTURE.md) for a detailed explanation of the system design, data flows, and key algorithms.
