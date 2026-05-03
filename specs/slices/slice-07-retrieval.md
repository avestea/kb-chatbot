# Slice 7 — Retrieval Function

**Depends on:** Slice 1, Slice 6
**Prereq:** Read `00-prompt-prefix.md` first.

> Pure function: given a chatbot id and a user question, return the most relevant chunks. No HTTP, no LLM, no streaming. Slice 8 builds the chat endpoint on top of it.

---

## Goal

A reusable, well-tested retrieval function that does the vector search half of RAG.

## Deliverables

- `api/src/rag/retrieve.py` — `retrieve_context()` function (locked cross-slice contract).

## `api/src/rag/retrieve.py`

```python
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
    similarity: float  # cosine similarity in [0, 1]

async def retrieve_context(
    *,
    chatbot_id: str,
    query: str,
    top_k: int = 5,
    min_similarity: float = 0.75,
) -> list[RetrievedChunk]:
    """
    Embeds the query, runs pgvector cosine search, filters by min_similarity.
    Returns [] when nothing passes the threshold.
    chatbot_id must already be validated as belonging to the caller's tenant.
    """
    # Embed the query
    [query_embedding] = await embed_chunks([query])

    # Vector search SQL
    # Note: don't put similarity threshold in WHERE — it defeats HNSW pruning.
    # Filter by threshold in Python after fetching top_k candidates.
    sql = text("""
        SELECT c.id::text,
               c.document_id::text,
               d.filename AS document_name,
               c.content,
               1 - (c.embedding <=> :embedding ::vector) AS similarity
        FROM chunks c
        JOIN documents d ON d.id = c.document_id
        WHERE c.chatbot_id = :chatbot_id ::uuid
          AND c.embedding_model = :model
          AND d.deleted_at IS NULL
        ORDER BY c.embedding <=> :embedding ::vector
        LIMIT :top_k
    """)

    async with async_session() as db:
        result = await db.execute(sql, {
            "embedding": str(query_embedding),
            "chatbot_id": chatbot_id,
            "model": EMBEDDING_MODEL,
            "top_k": top_k,
        })
        rows = result.mappings().all()

    return [
        RetrievedChunk(
            id=row["id"],
            document_id=row["document_id"],
            document_name=row["document_name"],
            content=row["content"],
            similarity=float(row["similarity"]),
        )
        for row in rows
        if float(row["similarity"]) >= min_similarity
    ]
```

## Implementation notes

### pgvector embedding parameter format

The embedding must be passed as a string in pgvector's array literal format: `'[0.1, 0.2, ...]'`. SQLAlchemy's `text()` with `:embedding` and a Python list won't work directly — convert first:

```python
embedding_str = "[" + ",".join(str(x) for x in query_embedding) + "]"
# Then pass: "embedding": embedding_str
```

### Why filter in Python, not SQL

Putting `WHERE 1 - (c.embedding <=> ...)  > 0.75` in the SQL causes PostgreSQL to compute the similarity for every row before applying the filter — it defeats the HNSW index. Instead, let HNSW do a fast approximate `ORDER BY ... LIMIT N`, then filter the small result set in Python.

### Why filter by `embedding_model`

If you upgrade embedding models later (e.g., from `text-embedding-3-small` to a hypothetical v2), old chunks have vectors in a different space. Filtering by model prevents garbage similarity scores across incompatible spaces.

## Acceptance criteria

- With a seeded chatbot containing a document about "company refund policy", calling `retrieve_context(chatbot_id=..., query='how do I get a refund')` returns ≥ 1 chunk with `similarity > 0.75`.
- Off-topic query `'what is the airspeed velocity of an unladen swallow'` returns `[]`.
- A chunk from a different chatbot is never returned (chatbot_id filter works).
- A chunk from a soft-deleted document is never returned (`d.deleted_at IS NULL` filter works).
