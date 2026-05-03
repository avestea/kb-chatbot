# Knowledge Base Chatbot Builder — Python Architecture

> **How to use this document**
> Each vertical slice is a self-contained implementation unit. Feed slices to your LLM in order. Start with Slice 0 — everything else builds on it.

---

## Tech Stack Decisions

| Layer | Choice | Reason |
|---|---|---|
| Backend | Python 3.12 + FastAPI + Uvicorn | Async, type-safe, Pydantic-native |
| Database | PostgreSQL + pgvector | One DB for relational data and vector embeddings |
| ORM | SQLAlchemy 2.0 async + Alembic | Mature async ORM; Alembic for migrations |
| Validation | Pydantic v2 | FastAPI's native validation layer |
| Embeddings | OpenAI `text-embedding-3-small` | Cheap, 1536-dim, high quality |
| LLM | Anthropic Claude Sonnet 4.6 (`claude-sonnet-4-6`) | Strong grounded answers |
| File parsing | pdfplumber (PDF), python-docx (DOCX), BeautifulSoup4 (HTML) | Best-in-class Python parsers |
| Auth | Clerk | Multi-tenant JWT auth + webhook lifecycle |
| UI | Gradio | Python-native chat + file upload UI. Replaces both Next.js dashboard and the embeddable widget |
| Queue | ARQ + Redis | Async Redis queue — Python equivalent of BullMQ |
| Storage | S3 / MinIO + aioboto3 | Original file storage; async boto3 |
| Dev environment | Docker + Docker Compose | All services containerised |

---

## Data Model

```python
# Core Pydantic types — used in API responses across all slices
# Canonical SQLAlchemy ORM lives in api/src/db/models.py

from pydantic import BaseModel
from typing import Literal
from uuid import UUID
from datetime import datetime

class Tenant(BaseModel):
    id: UUID
    name: str
    plan: Literal['free', 'starter', 'pro', 'business']
    clerk_user_id: str
    created_at: datetime

class Chatbot(BaseModel):
    id: UUID
    tenant_id: UUID
    name: str
    system_prompt_override: str | None
    created_at: datetime

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
    created_at: datetime

class Chunk(BaseModel):
    id: UUID
    document_id: UUID
    chatbot_id: UUID
    tenant_id: UUID
    content: str
    token_count: int
    chunk_index: int
    embedding_model: str
    # embedding vector omitted from API responses — too large

class Conversation(BaseModel):
    id: UUID
    chatbot_id: UUID
    session_id: str
    created_at: datetime

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
```

---

## Slice Overview

| Slice | Title | Key deliverable |
|---|---|---|
| 0 | Docker / Dev Environment | All services in compose; developer needs only Docker |
| 1 | FastAPI Scaffold + DB Schema | Server boots, DB schema migrated, `/health` works |
| 2 | Auth & Multi-Tenancy | Demo-first auth (`AUTH_MODE=demo`); Clerk-ready for prod |
| 3 | Chatbot CRUD | Full REST CRUD for chatbots |
| 4 | Document Upload | Multipart upload → S3 → ARQ job enqueued |
| 5 | Parsing Worker | ARQ worker: S3 → parse → plain text |
| 6 | Chunk + Embed + Persist | text → chunks → OpenAI embeddings → pgvector |
| 7 | Retrieval Function | `retrieve_context()` vector search |
| 8 | Chat Endpoint | `POST /api/v1/chat/:id/message` SSE streaming + Claude |
| 9 | Gradio UI | Python dashboard: chatbot mgmt + doc upload + chat |
| 10 | Explainability | Sources panel: which chunks answered the question + similarity scores |
| 11 | Evaluation Dashboard | No-answer rate, failure browser, conversation inspector |
| 12 | Hybrid Search | BM25 + vector search merged with Reciprocal Rank Fusion |
| 13 | Query Rewriting | Rewrite follow-up questions into self-contained retrieval queries |
| 14 | Feedback | Thumbs up/down per answer; satisfaction rate in evaluation dashboard |

**Minimum viable product:** Slices 0–9. After these, you have a working RAG chatbot with a Python UI.

**Recommended next layer:** Slices 10–11 add observability into answer quality with no new infrastructure.

**Quality layer:** Slices 12–14 improve retrieval accuracy and collect user signal.

---

## Implementation Order

```
Slice 0  (Docker)
   └── Slice 1  (scaffold)
         └── Slice 2  (auth)
               └── Slice 3  (chatbot CRUD)
                     └── Slice 4  (document upload)
                           └── Slice 5  (parsing worker)
                                 └── Slice 6  (chunk + embed)
                                       └── Slice 7  (retrieval)
                                             └── Slice 8  (chat endpoint)
                                                   └── Slice 9  (Gradio UI)
                                                         ├── Slice 10 (explainability)
                                                         │     └── Slice 11 (evaluation dashboard) — needs Slice 10
                                                         │           └── Slice 14 (feedback) — extends Slice 11
                                                         ├── Slice 12 (hybrid search) — extends Slice 7
                                                         └── Slice 13 (query rewriting) — extends Slice 8
```

---

## Prompting Tips

Prepend `00-prompt-prefix.md` before every slice prompt. It contains the full tech stack, data model, file layout, and cross-slice contracts.

```
Read 00-prompt-prefix.md then implement: Slice N — [title]
```

Feed one slice at a time. Verify acceptance criteria before proceeding.
