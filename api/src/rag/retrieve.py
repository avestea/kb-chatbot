from dataclasses import dataclass
from sqlalchemy import text
from src.db.base import async_session
from src.lib.embedder import embed_chunks, EMBEDDING_MODEL

RRF_K = 60  # standard constant; higher = smoother ranking, less sensitive to top positions


@dataclass
class RetrievedChunk:
    id: str
    document_id: str
    document_name: str
    content: str
    similarity: float  # cosine similarity from vector search; 0.0 for keyword-only hits


async def retrieve_context(
    *,
    chatbot_id: str,
    query: str,
    top_k: int = 5,
    min_similarity: float = 0.4,
) -> list[RetrievedChunk]:
    """
    Hybrid BM25 + vector retrieval merged with Reciprocal Rank Fusion.
    Keyword-only hits (no vector match above threshold) pass through with similarity=0.0.
    Returns [] when nothing passes the threshold or FTS matches.
    """
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
        "limit": top_k * 3,
    }

    # Run sequentially — SQLAlchemy async sessions are not safe for concurrent coroutines.
    async with async_session() as db:
        vec_rows = (await db.execute(vector_sql, params)).mappings().all()
        fts_rows = (await db.execute(fts_sql, params)).mappings().all()

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

    # Track FTS hits so chunks found by keyword can bypass the vector similarity floor.
    fts_ids = {row["id"] for row in fts_rows}

    results = []
    for item in merged:
        row = item["row"]
        in_fts = row["id"] in fts_ids
        vec_similarity = float(row["similarity"]) if row["similarity"] is not None else None

        # Pure vector hit that doesn't meet the similarity threshold: skip.
        if vec_similarity is not None and vec_similarity < min_similarity and not in_fts:
            continue

        # Keyword hit: either FTS-only row (no vector score) or vector similarity was
        # below threshold but FTS rescued it — both surface as similarity = 0.0.
        if in_fts and (vec_similarity is None or vec_similarity < min_similarity):
            similarity = 0.0
        else:
            similarity = vec_similarity if vec_similarity is not None else 0.0

        results.append(RetrievedChunk(
            id=row["id"],
            document_id=row["document_id"],
            document_name=row["document_name"],
            content=row["content"],
            similarity=similarity,
        ))

    return results
