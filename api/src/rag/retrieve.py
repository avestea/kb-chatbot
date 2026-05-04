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
    [query_embedding] = await embed_chunks([query])

    # pgvector array literal format: '[x1,x2,...]'
    embedding_str = "[" + ",".join(str(x) for x in query_embedding) + "]"

    # Filter threshold is applied in Python after fetching top_k candidates —
    # putting it in WHERE would defeat the HNSW index by forcing a full scan.
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
            "embedding": embedding_str,
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
