# Slice 12 — Hybrid Search (BM25 + Vector)

**Depends on:** Slice 7 (retrieval function)
**Prereq:** Read `00-prompt-prefix.md` first.

---

## Goal

Replace pure vector search with a combination of semantic (vector) and keyword (BM25/full-text) search, merged with Reciprocal Rank Fusion. This improves retrieval for proper nouns, product codes, section references, and any case where the exact word matters more than its meaning.

Everything stays in PostgreSQL — no new infrastructure.

---

## Why it helps

| Query type | Pure vector | Hybrid |
|---|---|---|
| "refund policy" | ✓ good | ✓ good |
| "Section 4.2" | ✗ poor — embedding doesn't know that's a locator | ✓ good |
| "SKU-8821 warranty" | ✗ poor | ✓ good |
| "what does the CEO say about growth" | ✓ good | ✓ good |

---

## Schema change

Add a generated `tsvector` column to `chunks`. PostgreSQL maintains it automatically on insert/update.

```sql
-- New migration
ALTER TABLE chunks
  ADD COLUMN content_tsv tsvector
  GENERATED ALWAYS AS (to_tsvector('english', content)) STORED;

CREATE INDEX chunks_content_tsv_idx ON chunks USING gin(content_tsv);
```

Generate and apply:
```bash
docker compose exec api alembic revision -m "add_chunks_tsvector"
# Hand-append the SQL above to the generated migration file
docker compose exec api alembic upgrade head
```

Add to `models.py` (informational — SQLAlchemy won't manage the generated column, but declare it so queries can reference it):
```python
from sqlalchemy import Computed
from sqlalchemy.dialects.postgresql import TSVECTOR

class Chunk(Base):
    ...
    content_tsv: Mapped[Any] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('english', content)", persisted=True),
        init=False,
    )
```

---

## Updated retrieval (`api/src/rag/retrieve.py`)

Replace the single vector query with two parallel queries + RRF merge.

```python
import asyncio
from dataclasses import dataclass
from sqlalchemy import text
from src.db.base import async_session
from src.lib.embedder import embed_chunks, EMBEDDING_MODEL

@dataclass
class RetrievedChunk:
    id: str
    document_id: str
    document_name: str
    content: str
    similarity: float      # cosine similarity from vector search; 0.0 if keyword-only hit

RRF_K = 60  # standard constant; higher = smoother ranking, less sensitive to top positions

async def retrieve_context(
    *,
    chatbot_id: str,
    query: str,
    top_k: int = 5,
    min_similarity: float = 0.75,
) -> list[RetrievedChunk]:
    [query_embedding] = await embed_chunks([query])
    embedding_str = "[" + ",".join(str(x) for x in query_embedding) + "]"

    vector_sql = text("""
        SELECT c.id::text,
               c.document_id::text,
               d.filename          AS document_name,
               c.content,
               1 - (c.embedding <=> :emb ::vector) AS similarity,
               ROW_NUMBER() OVER (ORDER BY c.embedding <=> :emb ::vector) AS rank
        FROM chunks c
        JOIN documents d ON d.id = c.document_id
        WHERE c.chatbot_id = :chatbot_id ::uuid
          AND c.embedding_model  = :model
          AND d.deleted_at IS NULL
        ORDER BY c.embedding <=> :emb ::vector
        LIMIT :limit
    """)

    fts_sql = text("""
        SELECT c.id::text,
               c.document_id::text,
               d.filename          AS document_name,
               c.content,
               NULL::float         AS similarity,
               ROW_NUMBER() OVER (
                   ORDER BY ts_rank_cd(c.content_tsv,
                            plainto_tsquery('english', :query_text)) DESC
               ) AS rank
        FROM chunks c
        JOIN documents d ON d.id = c.document_id
        WHERE c.chatbot_id = :chatbot_id ::uuid
          AND d.deleted_at IS NULL
          AND c.content_tsv @@ plainto_tsquery('english', :query_text)
        ORDER BY ts_rank_cd(c.content_tsv,
                 plainto_tsquery('english', :query_text)) DESC
        LIMIT :limit
    """)

    params = {
        "emb": embedding_str,
        "chatbot_id": chatbot_id,
        "model": EMBEDDING_MODEL,
        "query_text": query,
        "limit": top_k * 3,   # fetch more candidates before merging
    }

    async with async_session() as db:
        vec_rows, fts_rows = await asyncio.gather(
            db.execute(vector_sql, params).then(lambda r: r.mappings().all()),
            db.execute(fts_sql, params).then(lambda r: r.mappings().all()),
        )

    # RRF merge
    scores: dict[str, dict] = {}

    for row in vec_rows:
        rid = row["id"]
        scores[rid] = {"row": dict(row), "rrf": 0.0}
        scores[rid]["rrf"] += 1.0 / (RRF_K + row["rank"])

    for row in fts_rows:
        rid = row["id"]
        if rid not in scores:
            scores[rid] = {"row": dict(row), "rrf": 0.0}
        scores[rid]["rrf"] += 1.0 / (RRF_K + row["rank"])

    merged = sorted(scores.values(), key=lambda x: x["rrf"], reverse=True)[:top_k]

    results = []
    for item in merged:
        row = item["row"]
        similarity = float(row["similarity"]) if row["similarity"] is not None else 0.0

        # Filter: vector-matched chunks must meet min_similarity.
        # Keyword-only hits (similarity == 0.0) pass through — they matched exact terms.
        if row["similarity"] is not None and similarity < min_similarity:
            continue

        results.append(RetrievedChunk(
            id=row["id"],
            document_id=row["document_id"],
            document_name=row["document_name"],
            content=row["content"],
            similarity=similarity,
        ))

    return results
```

> **asyncio.gather note:** SQLAlchemy async sessions are not safe to share across concurrent coroutines. Open two separate sessions or run the queries sequentially. Sequential is fine — both queries are fast (indexed). Replace the `gather` with sequential `await` calls if you hit session errors:
> ```python
> async with async_session() as db:
>     vec_rows = (await db.execute(vector_sql, params)).mappings().all()
>     fts_rows = (await db.execute(fts_sql, params)).mappings().all()
> ```

---

## No changes needed downstream

`retrieve_context` signature is unchanged — `RetrievedChunk` has the same fields. The chat endpoint, sources SSE event, and evaluation dashboard all work without modification.

The only visible difference: chunks that matched on exact keywords but scored below the cosine similarity threshold now appear in results (with `similarity=0.0` displayed as a keyword match in the sources panel).

---

## Optional: label keyword-only hits in the sources panel

In `slice-10-explainability.md`, the sources panel shows `similarity` as a number. Keyword-only hits have `0.0`, which looks wrong. Add a `match_type` field to the SSE payload:

```python
sources_payload = [
    {
        ...
        "similarity": round(c.similarity, 3),
        "match_type": "keyword" if c.similarity == 0.0 else "semantic",
    }
    ...
]
```

The Gradio sources dataframe can show "keyword" or "semantic" instead of a raw 0.0.

---

## Acceptance criteria

- Upload a document containing "SKU-8821 warranty terms". Query `"SKU-8821"` returns the relevant chunk (keyword match).
- Semantic queries ("how do I get a refund") still work as before.
- `retrieve_context` returns at most `top_k` results.
- The `content_tsv` column is populated automatically on chunk insert — no manual update needed.
- No changes needed to Slices 8, 10, or 11.
