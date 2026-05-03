# What This Project Is and How It Works
### A beginner-friendly walkthrough

---

## The Big Picture

This project is a **knowledge base chatbot builder**. Think of it like a mini-version of what companies use when they want a chat widget on their website that can answer questions about *their specific content* — user manuals, internal docs, FAQs, etc.

Here's the flow from a user's perspective:

```
1. An operator uploads documents (PDF, Word, HTML, plain text)
2. The system reads, chunks, and indexes those documents
3. An end-user asks a question in a chat box
4. The system finds the most relevant chunks from the documents
5. It feeds those chunks + the question to an AI (Claude) to generate an answer
```

This technique — searching your own documents and feeding relevant pieces to an AI — is called **RAG** (Retrieval-Augmented Generation). It's how you make an AI that "knows" your specific content without training a new model.

---

## Multi-Tenancy: One App, Many Customers

This is a **SaaS** (Software as a Service) app, meaning many different companies ("tenants") share the same running software but each see only their own data.

Every database table has a `tenant_id` column. Every query filters by it. If Tenant A somehow sends Tenant B's chatbot ID, the server returns 404 — as if it doesn't exist.

```
Tenant A  →  their chatbots, their documents, their chunks
Tenant B  →  their chatbots, their documents, their chunks
              (completely separate, never mixed)
```

---

## The Tech Stack — What Each Piece Does

### Docker (the container system)
Everything runs inside Docker containers. You don't install Python, Postgres, or Redis on your machine — Docker creates isolated boxes for each service. `docker compose up -d` starts all 7 services at once.

### PostgreSQL + pgvector (the database)
PostgreSQL is a standard relational database — it stores tenants, chatbots, documents, etc. in tables with rows and columns.

**pgvector** is a plugin that adds a special column type: `vector(1536)`. A vector is just a list of 1536 numbers that represents the *meaning* of a piece of text. Two chunks that mean similar things will have similar vectors. This lets the database find "most relevant" chunks using math (cosine similarity) instead of keyword matching.

### FastAPI (the web framework)
FastAPI is a Python library for building HTTP APIs. When your browser or app sends a request like `POST /api/v1/chatbots`, FastAPI routes it to the right Python function. It also handles:
- Validating inputs (via Pydantic)
- Converting Python objects to JSON
- Returning errors with the right HTTP status codes

### SQLAlchemy (the ORM)
An ORM (Object-Relational Mapper) lets you write Python classes instead of raw SQL. Instead of:
```sql
SELECT * FROM documents WHERE tenant_id = '...' AND deleted_at IS NULL
```
you write:
```python
select(Document).where(tenant_where(Document, tenant_id))
```

This project uses the **async** version — database queries don't block while waiting for results.

### Alembic (database migrations)
When the data model changes (e.g. adding a column), you can't just edit the Python class — the real database table needs updating too. Alembic tracks these changes as numbered migration files (`0001_init.py`, etc.) and applies them in order. Think of it like version control for your database schema.

### ARQ + Redis (the job queue)
When a document is uploaded, parsing it can take seconds (or longer for big PDFs). You don't want the HTTP request to sit there waiting. Instead:

1. The API saves the document record and immediately returns `{"status": "pending"}`
2. It drops a job onto a Redis queue: "hey, process document X"
3. A separate **worker** process picks up that job and does the heavy lifting

Redis is the message broker in the middle. ARQ is the Python library that wraps Redis into a proper job queue with retries, timeouts, etc.

```
API process  →  enqueue job  →  Redis  →  Worker process picks it up
                                           ↓
                                           Downloads file from S3
                                           Parses text
                                           (Slice 6: chunks + embeds)
                                           Updates Document.status
```

### MinIO (file storage)
MinIO is a self-hosted version of Amazon S3. Uploaded files (PDFs, DOCXs, etc.) are stored here as objects with a key like `tenant_id/chatbot_id/doc_id/filename.pdf`. In production you'd swap MinIO for real AWS S3 — the code doesn't change because both speak the same S3 API.

### OpenAI Embeddings (turning text into vectors)
The `text-embedding-3-small` model takes a string of text and returns a list of 1536 numbers (a vector/embedding). Similar sentences produce similar vectors. This is how semantic search works — you embed the user's question and find document chunks whose vectors are closest.

### Anthropic Claude (the LLM)
Claude (`claude-sonnet-4-6`) is the AI that generates the final answer. It receives a system prompt, the relevant document chunks found by the search, and the conversation history. It streams back a response token by token (like watching text appear in ChatGPT).

### Pydantic (data validation)
Pydantic models are Python classes that validate data automatically. If an API receives `{"name": 123}` when it expects a string, Pydantic catches it and returns a 422 error. All request bodies and response shapes are defined as Pydantic models.

### Structlog (logging)
Instead of `print("something happened")`, structlog writes structured log lines with timestamps, log levels, and key-value context — easier to search and monitor in production.

---

## The File Layout

```
kb-chatbot/
├── docker-compose.yml       Defines all 7 services and how they connect
├── .env                     Environment variables (DB password, API keys, etc.)
│
├── api/                     The Python backend
│   ├── pyproject.toml       Python dependencies list
│   ├── alembic/             Database migration files
│   └── src/
│       ├── main.py          FastAPI app — routes registered, errors handled
│       ├── config/
│       │   └── env.py       Reads .env into a typed Python object (Settings)
│       ├── db/
│       │   ├── base.py      Creates the database engine and session factory
│       │   ├── models.py    SQLAlchemy table definitions (Tenant, Chatbot, Document…)
│       │   └── tenant_scope.py  Helper: adds "WHERE tenant_id=X AND deleted_at IS NULL"
│       ├── auth/
│       │   ├── authenticate.py  FastAPI dependency — reads Bearer token, finds tenant
│       │   └── clerk.py         Token verification logic (demo mode or Clerk JWT)
│       ├── lib/
│       │   ├── s3.py        Wrapper around the S3/MinIO client
│       │   ├── redis.py     Redis connection pool
│       │   ├── errors.py    Custom exception classes (NotFoundError, etc.)
│       │   └── log.py       Structlog setup
│       ├── routes/
│       │   ├── chatbots.py  CRUD endpoints for chatbots
│       │   └── documents.py Upload/list/delete endpoints for documents
│       ├── worker/
│       │   ├── __init__.py  WorkerSettings — tells ARQ what functions to run
│       │   ├── jobs.py      ingest_document: download → parse → update status
│       │   └── parsers/
│       │       ├── pdf.py   Extract text from PDFs with pdfplumber
│       │       ├── docx.py  Extract text from Word files with python-docx
│       │       ├── html.py  Extract text from HTML with BeautifulSoup
│       │       └── txt.py   Read plain text files
│       └── schemas/
│           ├── chatbots.py  Pydantic shapes for chatbot API requests/responses
│           └── documents.py Pydantic shapes for document API requests/responses
│
├── web/
│   └── app.py               Gradio UI (chat interface — Slice 9)
│
└── infra/
    ├── postgres/init.sql    Runs once: enables the pgvector extension
    └── minio/init.sh        Runs once: creates the file storage bucket
```

---

## The Data Model

These are the main database tables and how they relate:

```
Tenant
  └── Chatbot (many per tenant)
        └── Document (many per chatbot)
              └── Chunk (many per document — the actual text pieces + their vectors)

Conversation (one per chat session)
  └── Message (the back-and-forth messages)
```

**Soft deletes:** Nothing is actually deleted from the database. Instead, `deleted_at` is set to the current timestamp. Every query checks `WHERE deleted_at IS NULL`. This means you can recover data and keep audit trails.

---

## How a Request Flows Through the Code

### Example: uploading a document

```
1. HTTP POST /api/v1/chatbots/{id}/documents
   with Authorization: Bearer alice
   with file: report.pdf

2. FastAPI routes to upload_document() in routes/documents.py

3. get_current_tenant() dependency runs:
   - Reads "alice" from Bearer header
   - Looks up (or creates) the Tenant for "alice"
   - Returns AuthenticatedTenant(tenant_id=..., user_id="alice")

4. The function checks the chatbot belongs to alice's tenant (404 if not)

5. Validates MIME type (415 if not PDF/DOCX/HTML/TXT)

6. Reads file bytes, checks size (413 if > 20MB)

7. Uploads bytes to MinIO/S3

8. Inserts a Document row into Postgres with status="pending"

9. Enqueues an ARQ job: ingest_document(document_id=...)

10. Returns 201 {"document": {"id": "...", "status": "pending", ...}}

--- Meanwhile, in the worker process ---

11. ingest_document() picks up the job from Redis

12. Fetches the Document from Postgres, sets status="processing"

13. Downloads the file bytes from S3

14. Calls the right parser (parse_pdf for PDFs, etc.)

15. Gets back plain text

16. (Slice 6) Splits text into chunks, embeds each chunk via OpenAI,
    saves chunks with their vectors to Postgres

17. Sets status="ready"
```

---

## Auth: Demo Mode vs. Production

The app has two auth modes, controlled by `AUTH_MODE` in `.env`:

**Demo mode** (`AUTH_MODE=demo`):
- Any Bearer token value works
- The token value itself is the user identity
- `Authorization: Bearer alice` → creates/finds tenant "alice"
- Good for local development and testing

**Clerk mode** (`AUTH_MODE=clerk`):
- Bearer token must be a real JWT signed by Clerk
- JWT is verified against Clerk's public keys
- The user's real ID is extracted from the JWT

The `get_current_tenant()` function handles both cases transparently — the rest of the code just gets back a `tenant_id` and doesn't care how it was obtained.

---

## The RAG Pipeline (Retrieval-Augmented Generation)

This is the core of why the chatbot actually works:

```
User asks: "What is the return policy?"

1. EMBED the question
   → OpenAI turns it into a vector: [0.02, -0.15, 0.88, ...]

2. SEARCH in Postgres using pgvector
   → Find the 5 chunks whose vectors are most similar to the question vector
   → These are the most semantically relevant pieces of text

3. BUILD a prompt for Claude:
   System: "You are a helpful assistant. Answer using only these documents:
            [chunk 1 text] [chunk 2 text] [chunk 3 text]..."
   User:   "What is the return policy?"

4. STREAM Claude's response back to the user token by token

5. SAVE the conversation and message to Postgres
```

The key insight: Claude doesn't "know" the company's documents. But by injecting relevant chunks into the prompt at query time, Claude can answer as if it does.

---

## What's Been Built (May 2026)

| Slice | What it adds |
|---|---|
| 0 | Docker setup — all 7 services running |
| 1 | FastAPI scaffold, database schema, `/health` endpoint |
| 2 | Auth system — Bearer tokens, tenant auto-creation |
| 3 | Chatbot CRUD — create/list/update/delete chatbots |
| 4 | Document upload — upload files, store in S3, enqueue jobs |
| 5 | Parsing worker — worker processes jobs, extracts text from files |
| 6–9 | Chunking, embeddings, retrieval, chat endpoint, Gradio UI *(coming)* |

---

## Running It Locally

```bash
# 1. Copy the example environment file
cp .env.example .env

# 2. Start all services
docker compose up -d

# 3. Run database migrations
docker compose exec api alembic upgrade head

# 4. Check it's alive
curl localhost:8000/health
# → {"status":"ok","db":"connected","redis":"connected"}

# 5. Create a chatbot
curl -X POST localhost:8000/api/v1/chatbots \
  -H "Authorization: Bearer alice" \
  -H "Content-Type: application/json" \
  -d '{"name": "My Bot"}'

# 6. Run the tests
docker compose exec api pytest
```

The Gradio UI will be at `http://localhost:7860` once Slice 9 is built.
