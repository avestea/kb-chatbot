# Architecture

KB Chatbot is a multi-tenant SaaS RAG (Retrieval-Augmented Generation) system. Operators upload documents to a knowledge base; end-users ask questions and receive answers grounded in those documents. This document describes the system design, component responsibilities, data flows, and key algorithms.

## Table of Contents

1. [System Overview](#system-overview)
2. [Services](#services)
3. [Technology Stack](#technology-stack)
4. [Database Schema](#database-schema)
5. [API Routes](#api-routes)
6. [Document Ingestion Flow](#document-ingestion-flow)
7. [Chat Flow](#chat-flow)
8. [Retrieval Algorithm](#retrieval-algorithm)
9. [Query Rewriting](#query-rewriting)
10. [Feedback and Analytics](#feedback-and-analytics)
11. [Multi-Tenancy](#multi-tenancy)
12. [Authentication](#authentication)
13. [File Storage](#file-storage)
14. [Key Source Files](#key-source-files)

---

## System Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                         docker-compose                          │
│                                                                 │
│  ┌──────────┐    ┌──────────────────────────────────────────┐  │
│  │  Gradio  │    │              FastAPI (api)               │  │
│  │   Web    │───▶│  /api/v1/chatbots                        │  │
│  │   UI     │    │  /api/v1/chatbots/{id}/documents         │  │
│  │  :7860   │    │  /api/v1/chat/{id}/message  (SSE)        │  │
│  └──────────┘    │  /api/v1/analytics/*                     │  │
│                  │  /api/v1/feedback                        │  │
│                  │  /health                                 │  │
│                  └──────────────┬───────────────────────────┘  │
│                                 │                               │
│             ┌───────────────────┼───────────────────┐          │
│             │                   │                   │          │
│   ┌─────────▼──────┐   ┌────────▼───────┐  ┌───────▼──────┐  │
│   │  PostgreSQL 16 │   │   Redis 7      │  │   MinIO      │  │
│   │  + pgvector    │   │  (job queue)   │  │  (S3-compat) │  │
│   │    :5432       │   │    :6379       │  │  :9000/:9001 │  │
│   └────────────────┘   └────────┬───────┘  └──────────────┘  │
│                                 │                               │
│                        ┌────────▼───────┐                      │
│                        │  ARQ Worker    │                      │
│                        │  (worker svc)  │                      │
│                        └────────────────┘                      │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
                         │                │
              ┌──────────▼──┐    ┌────────▼──────┐
              │  OpenAI API  │    │ Anthropic API │
              │ (embeddings) │    │   (LLM chat)  │
              └─────────────┘    └───────────────┘
```

---

## Services

Seven Docker services are defined in `docker-compose.yml`:

| Service | Image | Role |
|---|---|---|
| `postgres` | postgres:16-alpine + pgvector | Relational database with vector similarity search |
| `redis` | redis:7-alpine | Job queue broker for ARQ |
| `minio` | minio/minio | S3-compatible object storage for uploaded files |
| `minio-init` | minio/mc | One-shot container that creates the `kbchat-dev` bucket |
| `api` | Custom (Python 3.12) | FastAPI HTTP server |
| `worker` | Same image as `api` | ARQ background worker (runs `arq src.worker.WorkerSettings`) |
| `ui` | Custom (Python 3.12) | Gradio web UI |

The `api` and `worker` services use the same Docker image. The entrypoint differs: `api` runs `uvicorn`, `worker` runs `arq`.

PostgreSQL is initialized with `infra/postgres/init.sql` which enables the `vector` extension. MinIO is initialized with `infra/minio/init.sh` which creates the bucket.

---

## Technology Stack

| Layer | Technology | Version | Purpose |
|---|---|---|---|
| Backend framework | FastAPI + Uvicorn | 0.115 / 0.32 | Async HTTP server |
| Language | Python | 3.12 | — |
| Database | PostgreSQL | 16 | Relational storage |
| Vector search | pgvector | 0.7 | HNSW index for cosine similarity |
| ORM | SQLAlchemy async | 2.0 | Async database queries |
| Migrations | Alembic | 1.14 | Schema versioning |
| Validation | Pydantic | v2 | Request/response models |
| Job queue | ARQ + Redis | — | Background document processing |
| File storage | MinIO / AWS S3 (aioboto3) | — | Document persistence |
| Embeddings | OpenAI text-embedding-3-small | 1536-dim | Vector representation of text chunks |
| LLM — chat | Anthropic Claude Sonnet 4.6 | — | Streaming RAG responses |
| LLM — rewrite | Anthropic Claude Haiku 4.5 | — | Follow-up query rewriting |
| Tokenizer | tiktoken (cl100k_base) | — | Token-aware text chunking |
| Document parsing | pdfplumber, python-docx, BeautifulSoup4 | — | Text extraction |
| UI | Gradio | 6.x | Python-native web interface |
| Logging | structlog | — | Structured JSON logs |
| Auth | Demo mode / Clerk | — | Multi-tenant JWT |
| Containerization | Docker + Compose v2 | — | All services |

---

## Database Schema

Six tables. Every table has `deleted_at TIMESTAMP NULL` for soft deletes. All queries add `WHERE deleted_at IS NULL`. Data is never physically deleted, enabling audit trails and recovery.

### `tenants`

The root of the multi-tenant hierarchy. One row per user/organization.

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | — |
| `name` | TEXT | Display name |
| `clerk_user_id` | TEXT UNIQUE | Identifies the user; token string in demo mode |
| `plan` | TEXT | `free` / `starter` / `pro` / `business` |
| `stripe_customer_id` | TEXT NULL | Billing (reserved) |
| `stripe_subscription_id` | TEXT NULL | Billing (reserved) |
| `created_at` | TIMESTAMP | — |
| `deleted_at` | TIMESTAMP NULL | Soft delete |

### `chatbots`

A chatbot belongs to one tenant and has its own knowledge base.

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | — |
| `tenant_id` | UUID FK → tenants | — |
| `name` | TEXT | — |
| `system_prompt_override` | TEXT NULL | Custom system prompt; falls back to default if NULL |
| `created_at` | TIMESTAMP | — |
| `deleted_at` | TIMESTAMP NULL | — |

### `documents`

A file uploaded to a chatbot. Tracks processing status.

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | — |
| `chatbot_id` | UUID FK → chatbots | — |
| `tenant_id` | UUID FK → tenants | Denormalized for access control queries |
| `filename` | TEXT | Original filename |
| `mime_type` | TEXT | `application/pdf`, `text/html`, etc. |
| `s3_key` | TEXT | Object path in MinIO/S3 |
| `status` | TEXT | `pending` → `processing` → `ready` \| `error` |
| `page_count` | INT NULL | Set after parsing |
| `error_reason` | TEXT NULL | Set if status = `error` |
| `created_at` | TIMESTAMP | — |
| `deleted_at` | TIMESTAMP NULL | — |

### `chunks`

A text segment derived from a document. Stores both the text and its vector embedding for retrieval.

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | — |
| `document_id` | UUID FK → documents | — |
| `chatbot_id` | UUID FK → chatbots | Denormalized for retrieval queries |
| `tenant_id` | UUID FK → tenants | Denormalized for access control |
| `content` | TEXT | Raw text of the chunk |
| `token_count` | INT | tiktoken token count |
| `chunk_index` | INT | Position within the document (0-based) |
| `embedding` | VECTOR(1536) | OpenAI text-embedding-3-small output |
| `embedding_model` | TEXT | `text-embedding-3-small` |
| `content_tsv` | TSVECTOR (generated) | Full-text search vector for `content` |

Indexes:
- HNSW index on `embedding` using cosine distance (via pgvector) — enables sub-linear approximate nearest-neighbor search
- GIN index on `content_tsv` — enables fast full-text search

### `conversations`

A chat session between a user and a chatbot. Identified by `(chatbot_id, session_id)`.

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | — |
| `chatbot_id` | UUID FK → chatbots | — |
| `session_id` | TEXT | Caller-provided session identifier |
| `created_at` | TIMESTAMP | — |

Unique constraint on `(chatbot_id, session_id)` — the chat endpoint upserts to resume an existing session.

### `messages`

One message (user or assistant) within a conversation.

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | — |
| `conversation_id` | UUID FK → conversations | — |
| `role` | TEXT | `user` \| `assistant` |
| `content` | TEXT | Message text |
| `source_chunks` | JSONB NULL | Retrieved chunks for assistant messages (see below) |
| `tokens_used` | INT NULL | Total tokens consumed (input + output) |
| `no_answer` | BOOL | `true` if the LLM replied that it had no relevant information |
| `prompt_version` | TEXT | `v1` (for prompt A/B tracking) |
| `created_at` | TIMESTAMP | — |

`source_chunks` JSONB shape (array):
```json
[
  {
    "index": 1,
    "chunk_id": "<UUID>",
    "document_name": "policy.pdf",
    "snippet": "Returns are accepted within 30 days...",
    "similarity": 0.923,
    "match_type": "semantic"
  }
]
```
`match_type` is `"semantic"` for vector matches and `"keyword"` for full-text-only matches (similarity = 0.0).

### `feedback`

One rating per assistant message. Upserts on `message_id`.

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | — |
| `message_id` | UUID FK → messages UNIQUE | One rating per message |
| `chatbot_id` | UUID FK → chatbots | Denormalized for analytics queries |
| `tenant_id` | UUID FK → tenants | Denormalized for access control |
| `rating` | SMALLINT | `1` (thumbs up) or `-1` (thumbs down) |
| `created_at` | TIMESTAMP | — |
| `updated_at` | TIMESTAMP | Updated on upsert |

---

## API Routes

All management routes require `Authorization: Bearer <token>`. The chat endpoint is public.

### Health

```
GET  /health
     → {"status": "ok", "db": "connected", "redis": "connected"}
```

### Webhooks

```
POST /webhooks/clerk
     → Receives Clerk lifecycle events (user.created, etc.)
     → In demo mode: acknowledges with 200 and no action
     → In clerk mode: verifies Svix signature, upserts tenant
```

### Chatbots

```
POST   /api/v1/chatbots               Create a chatbot
GET    /api/v1/chatbots               List chatbots (paginated; ?limit=&offset=)
GET    /api/v1/chatbots/{id}          Fetch one chatbot
PUT    /api/v1/chatbots/{id}          Update name or system_prompt_override
DELETE /api/v1/chatbots/{id}          Soft-delete chatbot (and its documents/chunks)
```

### Documents

```
POST   /api/v1/chatbots/{chatbot_id}/documents              Upload a file
GET    /api/v1/chatbots/{chatbot_id}/documents              List documents (paginated)
DELETE /api/v1/chatbots/{chatbot_id}/documents/{doc_id}     Soft-delete + delete chunks
```

### Chat

```
POST /api/v1/chat/{chatbot_id}/message
     Body: {"message": "...", "session_id": "..."}
     Response: text/event-stream (SSE)
       event: meta    data: {"conversation_id":"...","source_count":N,"retrieval_query":"..."}
       event: sources data: {"sources":[...]}
       event: token   data: {"text":"..."}   (repeated)
       event: done    data: {"message_id":"..."}
```

### Feedback

```
POST /api/v1/feedback
     Body: {"message_id": "<UUID>", "rating": 1 | -1}
     → 201 {"message_id": "...", "rating": 1}
     (upsert — submitting again updates the existing rating)
```

### Analytics

```
GET /api/v1/analytics/summary
    ?chatbot_id=<UUID>   (optional filter)
    → {
        total_conversations, total_messages,
        no_answer_count, no_answer_rate,
        avg_top_similarity,
        total_feedback, thumbs_up, thumbs_down, satisfaction_rate
      }

GET /api/v1/analytics/conversations
    ?chatbot_id=<UUID>     (optional)
    ?no_answer_only=true   (optional — filter to failure conversations)
    ?limit=&offset=
    → {"items": [...], "total": N, "has_more": bool}

GET /api/v1/analytics/conversations/{id}/messages
    → {
        "messages": [
          {"role":"user","content":"...","created_at":"..."},
          {"role":"assistant","content":"...","source_chunks":[...],"rating":1|−1|null,"created_at":"..."}
        ]
      }
```

---

## Document Ingestion Flow

Uploading a document is a two-phase operation: an immediate HTTP response followed by asynchronous background processing.

```
Phase 1 — HTTP (synchronous, < 1 second)
─────────────────────────────────────────
Client
  │
  │  POST /api/v1/chatbots/{id}/documents
  │  Content-Type: multipart/form-data
  │
  ▼
FastAPI (api service)
  ├─ Validate auth → resolve tenant
  ├─ Check MIME type (pdf/docx/html/txt) and file size
  ├─ Upload bytes → MinIO at key:  {tenant_id}/{chatbot_id}/{doc_id}/{filename}
  ├─ INSERT Document row (status = "pending")
  ├─ Enqueue ARQ job: ingest_document(document_id) → Redis
  └─ Return 201 {"document": {"id":"...","status":"pending",...}}


Phase 2 — Background worker (asynchronous, seconds to minutes)
───────────────────────────────────────────────────────────────
ARQ Worker
  ├─ Poll Redis for jobs
  ├─ Pick up ingest_document(document_id)
  ├─ UPDATE Document: status = "processing"
  ├─ Download file bytes from MinIO
  ├─ Parse text:
  │    .pdf  → pdfplumber  → plain text
  │    .docx → python-docx → plain text
  │    .html → BeautifulSoup4 → plain text (strip tags)
  │    .txt  → UTF-8 decode
  ├─ Chunk text:
  │    tiktoken cl100k_base tokenizer
  │    Target: ~400 tokens per chunk
  │    Splits on sentence boundaries when possible
  │    Overlap: none (clean boundaries)
  ├─ Embed chunks:
  │    OpenAI text-embedding-3-small
  │    Batch size: 100 chunks per API call
  │    Automatic retry on rate-limit errors
  │    Output: 1536-dimensional float vectors
  ├─ INSERT Chunk rows:
  │    content, token_count, chunk_index, embedding, content_tsv (auto-generated)
  └─ UPDATE Document: status = "ready" (or "error" + error_reason on failure)
```

---

## Chat Flow

The chat endpoint is a public SSE (Server-Sent Events) stream. No auth token is required — chatbots are embeddable in external sites.

```
Client
  │
  │  POST /api/v1/chat/{chatbot_id}/message
  │  {"message": "What is the refund policy?", "session_id": "s1"}
  │
  ▼
FastAPI (api service)
  │
  ├─ 1. Validate chatbot exists
  │
  ├─ 2. Upsert Conversation(chatbot_id, session_id)
  │       — resumes existing session or creates new one
  │
  ├─ 3. INSERT Message(role="user", content="What is the refund policy?")
  │
  ├─ 4. Load conversation history (prior user + assistant messages)
  │
  ├─ 5. Query rewriting (if Turn ≥ 2):
  │       Call Claude Haiku 4.5 with history + current message
  │       → Rewrite into self-contained retrieval query
  │       → e.g. "What is the refund policy?" (no change, Turn 1)
  │       → e.g. "What is the refund policy for digital goods?" (Turn 2, follow-up)
  │       Falls back to original query on any failure
  │
  ├─ 6. Retrieval (see Retrieval Algorithm section):
  │       embed(retrieval_query) → vector
  │       vector search (pgvector HNSW) → top k×3 candidates
  │       full-text search (plainto_tsquery) → top k×3 candidates
  │       RRF merge → top k results
  │       Filter: similarity ≥ 0.75 for vector matches
  │
  ├─ 7. Build sources payload:
  │       [{index, chunk_id, document_name, snippet, similarity, match_type}, ...]
  │
  ├─ 8. Build system prompt:
  │       "You are a helpful assistant. Answer using only the provided context."
  │       + Retrieved chunks injected as numbered passages
  │       + chatbot.system_prompt_override prepended if set
  │
  ├─ 9. Stream LLM response:
  │       Call Anthropic Claude Sonnet 4.6
  │       model = claude-sonnet-4-6
  │       messages = [{"role":"user"/"assistant","content":"..."}, ...] (full history)
  │       system = built system prompt
  │       stream = true
  │
  ├─ 10. SSE events emitted in order:
  │        event: meta    — conversation_id, source_count, retrieval_query
  │        event: sources — full sources array
  │        event: token   — one per LLM output token ({"text":"..."})
  │        event: done    — message_id of the saved assistant message
  │
  └─ 11. After streaming completes:
           INSERT Message(role="assistant", content, source_chunks JSONB,
                          tokens_used, no_answer, prompt_version)
```

### No-answer detection

The system prompt instructs the LLM: "If the context does not contain enough information to answer, say exactly: `I don't have information about that in the available documents.`"

After streaming, the assistant's response is checked for this phrase. If matched, `no_answer = true` is stored on the message. This drives the `no_answer_rate` analytics metric.

---

## Retrieval Algorithm

The retrieval function (`api/src/rag/retrieve.py`) uses **hybrid search**: combining dense vector similarity with sparse BM25-style full-text search, merged via **Reciprocal Rank Fusion (RRF)**.

### Why hybrid search?

| Query type | Pure vector | Hybrid |
|---|---|---|
| `"refund policy"` | Good | Good |
| `"Section 4.2"` | Poor (no semantic meaning) | Good (keyword match) |
| `"SKU-8821 warranty"` | Poor | Good |

Pure vector search fails on exact identifiers, codes, and proper nouns. Full-text search misses paraphrases and synonyms. Hybrid search handles both.

### Step-by-step

```
Input: retrieval_query (string), chatbot_id (UUID), k=5

1. Embed the query
   OpenAI text-embedding-3-small → float[1536]

2. Vector search (pgvector)
   SELECT id, content, 1 - (embedding <=> query_vec) AS similarity
   FROM chunks
   WHERE chatbot_id = ? AND deleted_at IS NULL
   ORDER BY embedding <=> query_vec
   LIMIT k * 3                   -- k=5 → 15 candidates

3. Full-text search (PostgreSQL)
   SELECT id, content, ts_rank_cd(content_tsv, query) AS rank
   FROM chunks
   WHERE chatbot_id = ? AND deleted_at IS NULL
     AND content_tsv @@ plainto_tsquery('english', retrieval_query)
   ORDER BY rank DESC
   LIMIT k * 3

4. Reciprocal Rank Fusion (RRF_K=60)
   For each chunk in either result set:
     rrf_score = 0
     if chunk in vector_results at position i:
       rrf_score += 1 / (60 + i)
     if chunk in fts_results at position j:
       rrf_score += 1 / (60 + j)

5. Sort by rrf_score DESC, take top k

6. Filter:
   - Vector-matched chunks: keep if similarity >= 0.75
   - Keyword-only chunks (not in vector results): keep unconditionally,
     set similarity = 0.0, match_type = "keyword"

7. Return RetrievedChunk objects:
   {chunk_id, content, similarity, match_type, document_name, chunk_index}
```

If no chunks pass the similarity threshold and no keyword hits, the LLM sees an empty context and triggers the no-answer response.

---

## Query Rewriting

Implemented in `api/src/rag/rewrite.py`.

### Problem

Follow-up questions contain pronouns and implicit references:
- Turn 1: "What is the refund policy?"
- Turn 2: "And for digital goods?" ← retrieval of this verbatim finds nothing

### Solution

Before retrieval, the system calls Claude Haiku 4.5 with the conversation history and asks it to rewrite the current query into a self-contained statement:
- Turn 2 rewritten: "What is the refund policy for digital goods?"

### Important properties

1. **Rewriting is invisible to the end user.** The original message is always sent to Claude Sonnet as the user's message. The rewrite only affects what is passed to the retrieval step.
2. **First turn is never rewritten.** No history exists, so no rewrite is attempted.
3. **Failures fall back silently.** If the Haiku call fails (network error, rate limit, timeout), the original query is used. The system never blocks on rewriting.
4. **Surfaced in metadata.** The `meta` SSE event includes `retrieval_query` only when the rewritten query differs from the original, so clients can optionally display it.

---

## Feedback and Analytics

### Feedback collection

Users rate each assistant response with 👍 (rating=1) or 👎 (rating=-1) via:
```
POST /api/v1/feedback {"message_id": "...", "rating": 1}
```
This upserts a row in the `feedback` table on the unique `message_id` constraint — re-voting updates the existing rating rather than creating a duplicate.

### Analytics metrics

The `summary` endpoint aggregates across all conversations (or filtered by chatbot):

| Metric | How it's computed |
|---|---|
| `total_conversations` | COUNT of conversations |
| `total_messages` | COUNT of messages where role = 'assistant' |
| `no_answer_count` | COUNT of messages where no_answer = true |
| `no_answer_rate` | no_answer_count / total_messages |
| `avg_top_similarity` | AVG of first element of source_chunks[0].similarity (JSONB) |
| `total_feedback` | COUNT of feedback rows |
| `thumbs_up` | COUNT of feedback where rating = 1 |
| `thumbs_down` | COUNT of feedback where rating = -1 |
| `satisfaction_rate` | thumbs_up / (thumbs_up + thumbs_down) |

### Failure inspection

The `conversations` endpoint accepts `?no_answer_only=true` to surface conversations where at least one message has `no_answer=true`. This helps identify knowledge gaps — questions users asked that the chatbot couldn't answer because the relevant documents weren't uploaded.

---

## Multi-Tenancy

Every piece of data is owned by a tenant. The hierarchy is:

```
Tenant
  └─ Chatbot (many per tenant)
       ├─ Document (many per chatbot)
       │    └─ Chunk (many per document)
       ├─ Conversation (many per chatbot)
       │    └─ Message (many per conversation)
       └─ Feedback (one per message)
```

Every table (except `conversations` and `messages`) stores `tenant_id` directly, denormalized for fast access control. This avoids joins when enforcing ownership.

The helper function `tenant_where(model, tenant)` in `api/src/db/tenant_scope.py` builds the WHERE clause added to every query. Attempting to access a resource owned by a different tenant returns HTTP 404, never 403 — this avoids leaking the existence of other tenants' data.

---

## Authentication

### Demo mode (`AUTH_MODE=demo`)

`api/src/auth/clerk.py` implements `verify_token(token)` by:
1. Looking up a `Tenant` where `clerk_user_id = token`
2. If not found, creating a new `Tenant` with `name = token`, `clerk_user_id = token`
3. Returning the tenant

Any Bearer token value is accepted. `alice` and `bob` are independent tenants with separate chatbots. This mode is suitable for local development and demos.

### Clerk mode (`AUTH_MODE=clerk`)

`verify_token(token)` verifies the JWT using the Clerk JWKS endpoint. The `clerk_user_id` is extracted from the token's `sub` claim. The stub in `api/src/lib/clerk_jwks.py` raises `NotImplementedError` and must be implemented before deploying with real Clerk auth.

The `POST /webhooks/clerk` endpoint handles lifecycle events (user created/deleted). In demo mode it returns 200 immediately. In Clerk mode it verifies the Svix webhook signature before processing.

### FastAPI dependency

```python
# Used as: current_tenant = Depends(get_current_tenant)
async def get_current_tenant(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: AsyncSession = Depends(get_db),
) -> Tenant:
    ...
```

Routes that require auth declare `Depends(get_current_tenant)`. The chat endpoint (`/api/v1/chat/{id}/message`) does **not** use this dependency — it is intentionally public so chatbots can be embedded in external websites without exposing API tokens.

---

## File Storage

Raw document files are stored in MinIO (development) or AWS S3 (production) at the key:

```
{tenant_id}/{chatbot_id}/{document_id}/{original_filename}
```

The `api/src/lib/s3.py` module creates a module-level aioboto3 session and exposes:
- `upload_file(key, data, content_type)` — uploads bytes
- `download_file(key)` — returns bytes
- `delete_file(key)` — deletes object

The S3 endpoint is configured via `S3_ENDPOINT`. Setting this to `http://minio:9000` (Docker internal hostname) routes to MinIO. Omitting it for production uses the native AWS S3 endpoint.

When a document or chatbot is soft-deleted, the S3 object is **not** deleted — only the `deleted_at` timestamp is set. This provides a recovery window. A future cleanup job could remove S3 objects for rows deleted more than N days ago.

---

## Key Source Files

| File | Purpose |
|---|---|
| `api/src/main.py` | FastAPI app, router registration, CORS, lifespan |
| `api/src/config/env.py` | `Settings` class — reads all environment variables via Pydantic |
| `api/src/config/chat.py` | Chat constants (`MAX_MESSAGE_TOKENS`, `TOP_K_CHUNKS`, etc.) |
| `api/src/db/models.py` | All SQLAlchemy ORM models |
| `api/src/db/base.py` | Async engine and session factory |
| `api/src/db/tenant_scope.py` | `tenant_where()` access control helper |
| `api/src/auth/authenticate.py` | `get_current_tenant` FastAPI dependency |
| `api/src/auth/clerk.py` | `verify_token()` — demo and Clerk implementations |
| `api/src/lib/llm.py` | `stream_completion()` — Anthropic streaming wrapper |
| `api/src/lib/embedder.py` | `embed_chunks()` — OpenAI batched embedding with retry |
| `api/src/lib/s3.py` | S3/MinIO upload/download/delete |
| `api/src/lib/redis.py` | ARQ Redis connection pool |
| `api/src/rag/retrieve.py` | `retrieve_context()` — hybrid BM25+vector+RRF retrieval |
| `api/src/rag/rewrite.py` | `rewrite_query()` — Haiku follow-up query rewriting |
| `api/src/rag/prompt.py` | `build_system_prompt()` — injects chunks into prompt |
| `api/src/routes/chat.py` | SSE chat endpoint |
| `api/src/routes/chatbots.py` | Chatbot CRUD |
| `api/src/routes/documents.py` | Document upload, list, delete |
| `api/src/routes/analytics.py` | Summary, conversations, message thread |
| `api/src/routes/feedback.py` | Feedback upsert |
| `api/src/worker/jobs.py` | `ingest_document()` ARQ job |
| `api/src/worker/chunker.py` | `chunk_text()` token-aware splitter |
| `api/src/worker/parsers/` | PDF, DOCX, HTML, TXT text extractors |
| `api/alembic/versions/` | 4 migration files (init, source_chunks, tsvector, feedback) |
| `web/app.py` | Gradio 4-tab UI |
| `web/api_client.py` | Typed httpx wrapper around the REST API |
| `docker-compose.yml` | All 7 services |
| `.env.example` | Environment variable template |
