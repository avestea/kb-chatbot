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

# Chat Endpoint (Slice 8+10) — public SSE stream, no auth required:
curl -N -X POST "localhost:8000/api/v1/chat/$BOT_ID/message" \
  -H "Content-Type: application/json" \
  -d '{"message":"What is the refund policy?","session_id":"my-session-1"}'
# → event: meta    data: {"conversation_id":"...","source_count":2}
# → event: sources data: {"sources":[{"index":1,"chunk_id":"...","document_name":"policy.pdf","similarity":0.92,"match_type":"semantic","snippet":"Returns accepted..."}]}
# → event: token   data: {"text":"Returns are"}
# → event: token   data: {"text":" accepted within 30 days."}
# → event: done    data: {"message_id":"..."}

# Gradio UI (Slice 9+10+11): http://localhost:7860
#   1. Enter "alice" in the API Token field
#   2. Chatbots tab → Refresh → see your chatbots; Create New Chatbot
#   3. Documents tab → Select Chatbot → Upload File → status "pending"
#      (worker processes it; click Refresh Documents until status is "ready")
#   4. Chat tab → Select Chatbot → type question → streaming answer appears
#      After each answer, a "Sources used" table shows document name,
#      similarity score, and snippet for each retrieved chunk.
#   5. Evaluation tab → Refresh → see quality stats (conversations, messages,
#      no-answer rate, avg similarity). Toggle "Show failures only" to filter
#      to conversations where the bot had no relevant context. Paste a
#      conversation ID and click Inspect to see the full message thread.

# Analytics API (Slice 11):
curl "localhost:8000/api/v1/analytics/summary" \
  -H "Authorization: Bearer alice"
# → {"total_conversations":5,"total_messages":8,"no_answer_count":2,"no_answer_rate":0.25,"avg_top_similarity":0.89}

curl "localhost:8000/api/v1/analytics/conversations?no_answer_only=true" \
  -H "Authorization: Bearer alice"
# → {"items":[{"id":"...","chatbot_id":"...","first_question":"What is...","has_failure":true,"created_at":"..."}],"total":2,"has_more":false}

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
  src/rag/retrieve.py         retrieve_context() — hybrid BM25+vector search merged with RRF (Slice 12)
  src/rag/rewrite.py          rewrite_query() — Haiku LLM rewrites follow-ups into self-contained queries (Slice 13)
  src/rag/prompt.py           build_system_prompt() — injects retrieved chunks into SYSTEM_TEMPLATE
  src/lib/llm.py              stream_completion() — Anthropic streaming wrapper (TokenEvent/UsageEvent)
  src/routes/chat.py          POST /api/v1/chat/{chatbot_id}/message — public SSE endpoint
  pyproject.toml              Dependencies
web/                          Gradio UI (Slice 9+11)
  app.py                      4-tab Gradio app: Chatbots / Documents / Chat / Evaluation
  api_client.py               Typed httpx wrapper around the FastAPI REST API
  requirements.txt            gradio>=5.0.0 (resolves to 6.x), httpx, python-dotenv
infra/
  postgres/init.sql           Enables pgvector extension
  minio/init.sh               Creates kbchat-dev bucket
specs/
  kb-chatbot-architecture.md  Full architecture reference
  progress.md                 Implementation status + LLM handoff notes
  slices/                     Slice-by-slice implementation prompts
```

## Query Rewriting (Slice 13)

Before embedding, follow-up queries are rewritten by `claude-haiku-4-5-20251001` into self-contained questions:

| Turn | Raw query | Rewritten for retrieval |
|---|---|---|
| 1 | "What is the refund policy?" | _(no rewrite — no history)_ |
| 2 | "And for digital goods?" | "What is the refund policy for digital goods?" |
| 3 | "Who do I contact?" | "Who do I contact to request a refund?" |

The LLM always sees the **original** message — the rewrite is invisible to the end user and only affects what is searched. If the rewrite fails for any reason, the original query is used. The `meta` SSE event includes `"retrieval_query"` when the query was changed.

## Retrieval Strategy (Slice 12)

Hybrid BM25 + vector search merged with Reciprocal Rank Fusion (RRF):

| Query type | Pure vector | Hybrid |
|---|---|---|
| "refund policy" | ✓ good | ✓ good |
| "Section 4.2" | ✗ poor | ✓ good |
| "SKU-8821 warranty" | ✗ poor | ✓ good |

- Vector search uses pgvector HNSW cosine distance (top `k×3` candidates)
- Full-text search uses PostgreSQL `plainto_tsquery` + `ts_rank_cd` (GIN index)
- RRF_K=60 merge, then top-k selected
- Keyword-only hits return with `similarity=0.0` and `match_type="keyword"` in the sources panel

## Implementation Status

See `specs/progress.md` for current slice status and LLM handoff context.
