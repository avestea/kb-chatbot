# Slice 6 — Chunk, Embed, and Persist

**Depends on:** Slice 1, Slice 5
**Prereq:** Read `00-prompt-prefix.md` first.

> Extends the worker from Slice 5. After the parser yields plain text, this slice chunks it, embeds the chunks via OpenAI, bulk-inserts them into pgvector, and moves `Document.status` to `ready`.

---

## Goal

Turn extracted text into embedded `chunks` rows, completing the document ingestion pipeline.

## Deliverables

- `api/src/worker/chunker.py` — `chunk_text(text)` pure function.
- `api/src/lib/embedder.py` — `embed_chunks(contents)` batched + retried.
- Extend `api/src/worker/jobs.py` to wire parse → chunk → embed → insert → `ready`.

## Chunker (`api/src/worker/chunker.py`)

```python
import re
from dataclasses import dataclass
import tiktoken

@dataclass
class DraftChunk:
    content: str
    token_count: int
    chunk_index: int

_enc = tiktoken.get_encoding("cl100k_base")

TARGET_TOKENS = 400
OVERLAP_TOKENS = 50
MIN_TOKENS = 20

def chunk_text(text: str) -> list[DraftChunk]:
    """Pure function. No I/O. Same input → same output."""
    sentences = re.split(r'(?<=[.!?])\s+', text)
    sentences = [s.strip() for s in sentences if s.strip()]

    chunks: list[DraftChunk] = []
    current: list[str] = []
    current_tokens = 0
    chunk_index = 0

    i = 0
    while i < len(sentences):
        sentence = sentences[i]
        tokens = len(_enc.encode(sentence))

        if current_tokens + tokens >= TARGET_TOKENS and current:
            content = " ".join(current)
            token_count = len(_enc.encode(content))
            if token_count >= MIN_TOKENS:
                chunks.append(DraftChunk(
                    content=content,
                    token_count=token_count,
                    chunk_index=chunk_index,
                ))
                chunk_index += 1

            # Overlap: walk back until we have ~50 overlap tokens
            overlap: list[str] = []
            overlap_tokens = 0
            for sent in reversed(current):
                t = len(_enc.encode(sent))
                if overlap_tokens + t > OVERLAP_TOKENS:
                    break
                overlap.insert(0, sent)
                overlap_tokens += t

            current = overlap
            current_tokens = overlap_tokens
        else:
            current.append(sentence)
            current_tokens += tokens
            i += 1

    # Final chunk
    if current:
        content = " ".join(current)
        token_count = len(_enc.encode(content))
        if token_count >= MIN_TOKENS:
            chunks.append(DraftChunk(
                content=content,
                token_count=token_count,
                chunk_index=chunk_index,
            ))

    return chunks
```

> **Known limitation:** the sentence-split regex splits on abbreviations (`Dr. Smith`, `e.g.`). Acceptable for MVP — chunk overlap heals most retrieval impact.

## Embedder (`api/src/lib/embedder.py`)

```python
from openai import AsyncOpenAI
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from src.config.env import settings

EMBEDDING_MODEL = "text-embedding-3-small"
EmbeddingVector = list[float]

_client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=4),
    retry=retry_if_exception_type(Exception),
    reraise=True,
)
async def _embed_batch(batch: list[str]) -> list[EmbeddingVector]:
    response = await _client.embeddings.create(
        model=EMBEDDING_MODEL,
        input=batch,
    )
    return [item.embedding for item in sorted(response.data, key=lambda x: x.index)]

async def embed_chunks(contents: list[str]) -> list[EmbeddingVector]:
    """Batch 100 per API call. Output order matches input order."""
    BATCH = 100
    results: list[EmbeddingVector] = []
    for i in range(0, len(contents), BATCH):
        batch = contents[i:i + BATCH]
        results.extend(await _embed_batch(batch))
    return results
```

## Extended job handler (`api/src/worker/jobs.py`)

Replace the Slice 5 placeholder with the full pipeline:

```python
async def ingest_document(ctx: dict, document_id: str):
    job_try = ctx.get("job_try", 1)

    async with async_session() as db:
        doc = (await db.execute(
            select(Document).where(Document.id == document_id)
        )).scalar_one_or_none()

        if doc is None:
            return

        doc.status = "processing"
        doc.error_reason = None
        await db.commit()

        try:
            # 1. Download from S3
            async with s3_client() as s3:
                response = await s3.get_object(Bucket=settings.S3_BUCKET, Key=doc.s3_key)
                file_bytes = await response["Body"].read()

            # 2. Parse
            parser = get_parser_for(doc.mime_type)
            text = _normalize_text(await parser(file_bytes, doc.filename))

            # 3. Chunk
            draft_chunks = chunk_text(text)
            if not draft_chunks:
                doc.status = "error"
                doc.error_reason = "document produced no chunks"
                await db.commit()
                return

            # 4. Idempotency: delete prior chunks on retry
            await db.execute(delete(Chunk).where(Chunk.document_id == doc.id))
            await db.commit()

            # 5. Embed + insert in batches of 100
            BATCH = 100
            for i in range(0, len(draft_chunks), BATCH):
                batch = draft_chunks[i:i + BATCH]
                embeddings = await embed_chunks([c.content for c in batch])
                db.add_all([
                    Chunk(
                        document_id=doc.id,
                        chatbot_id=doc.chatbot_id,
                        tenant_id=doc.tenant_id,
                        content=c.content,
                        token_count=c.token_count,
                        chunk_index=c.chunk_index,
                        embedding=emb,
                        embedding_model=EMBEDDING_MODEL,
                    )
                    for c, emb in zip(batch, embeddings)
                ])
                await db.commit()

            # 6. Mark ready
            doc.status = "ready"
            doc.error_reason = None
            await db.commit()
            log.info("document ingested", document_id=document_id, chunks=len(draft_chunks))

        except Exception as exc:
            err_msg = str(exc)[:500]
            log.error("ingest failed", document_id=document_id, error=err_msg, exc_info=exc)
            async with async_session() as db2:
                await db2.execute(
                    update(Document)
                    .where(Document.id == document_id)
                    .values(status="error", error_reason=err_msg)
                )
                await db2.commit()
            if job_try < 3:
                raise Retry(defer=5 ** job_try)
```

Add required imports at the top of `jobs.py`:
```python
from src.worker.chunker import chunk_text
from src.lib.embedder import embed_chunks, EMBEDDING_MODEL
from src.db.models import Chunk
from sqlalchemy import delete
```

## Acceptance criteria

- Upload a 10-page PDF → after worker runs, `chunks` table has rows with non-null 1536-dim `embedding` vectors, ascending `chunk_index`, `embedding_model = 'text-embedding-3-small'`.
- `Document.status` transitions: `pending` → `processing` → `ready`.
- `Document.error_reason` is null on success.
- Killing the worker mid-job and restarting: the next attempt deletes partial chunks and ingests cleanly.
- A failure during embedding → `status: error` with `error_reason` populated, partial chunks wiped.
- Large document (>1000 chunks) does not OOM the worker (batched insert verifies backpressure).
