import uuid
import pytest
from datetime import datetime, timezone
from unittest.mock import patch

from src.db.base import async_session_factory
from src.db.models import Tenant, Chatbot, Document, Chunk
from src.rag.retrieve import retrieve_context, RetrievedChunk

# Fake vectors used in tests
RELEVANT_VECTOR: list[float] = [0.1] * 1536     # stored in DB + returned for relevant query
OFFTOPIC_VECTOR: list[float] = [-0.1] * 1536    # cosine similarity = -1.0 vs RELEVANT_VECTOR


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------

async def _create_chatbot_with_chunk(
    embedding: list[float] = None,
    content: str = "Our company refund policy allows returns within 30 days.",
    doc_deleted: bool = False,
) -> tuple[str, str]:
    """Creates tenant + chatbot + document + chunk. Returns (chatbot_id, chunk_id)."""
    if embedding is None:
        embedding = RELEVANT_VECTOR[:]

    async with async_session_factory() as db:
        tenant = Tenant(
            name="Retrieval Test Tenant",
            plan="free",
            clerk_user_id=f"retrieve_{uuid.uuid4().hex[:12]}",
        )
        db.add(tenant)
        await db.flush()

        chatbot = Chatbot(tenant_id=tenant.id, name="Retrieval Test Bot")
        db.add(chatbot)
        await db.flush()

        doc = Document(
            chatbot_id=chatbot.id,
            tenant_id=tenant.id,
            filename="refund_policy.txt",
            mime_type="text/plain",
            s3_key=f"test/{uuid.uuid4().hex}/refund_policy.txt",
            status="ready",
            deleted_at=datetime.now(timezone.utc) if doc_deleted else None,
        )
        db.add(doc)
        await db.flush()

        chunk = Chunk(
            document_id=doc.id,
            chatbot_id=chatbot.id,
            tenant_id=tenant.id,
            content=content,
            token_count=12,
            chunk_index=0,
            embedding=embedding,
            embedding_model="text-embedding-3-small",
        )
        db.add(chunk)
        await db.commit()
        return str(chatbot.id), str(chunk.id)


# --------------------------------------------------------------------------------------
# Acceptance criteria tests
# --------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_retrieve_returns_relevant_chunk():
    """Relevant query returns ≥1 chunk with similarity > 0.75."""
    chatbot_id, chunk_id = await _create_chatbot_with_chunk()

    async def fake_embed(contents):
        return [RELEVANT_VECTOR[:] for _ in contents]

    with patch("src.rag.retrieve.embed_chunks", new=fake_embed):
        results = await retrieve_context(
            chatbot_id=chatbot_id,
            query="how do I get a refund",
        )

    assert len(results) >= 1
    assert all(isinstance(r, RetrievedChunk) for r in results)
    assert any(r.similarity > 0.75 for r in results)
    assert any(r.id == chunk_id for r in results)


@pytest.mark.asyncio
async def test_retrieve_off_topic_returns_empty():
    """Off-topic query (anti-parallel vector) returns []."""
    chatbot_id, _ = await _create_chatbot_with_chunk()

    async def fake_embed_offtopic(contents):
        return [OFFTOPIC_VECTOR[:] for _ in contents]

    with patch("src.rag.retrieve.embed_chunks", new=fake_embed_offtopic):
        results = await retrieve_context(
            chatbot_id=chatbot_id,
            query="what is the airspeed velocity of an unladen swallow",
        )

    assert results == []


@pytest.mark.asyncio
async def test_retrieve_chatbot_isolation():
    """Chunks from a different chatbot are never returned."""
    chatbot_a_id, chunk_a_id = await _create_chatbot_with_chunk()
    chatbot_b_id, chunk_b_id = await _create_chatbot_with_chunk()

    async def fake_embed(contents):
        return [RELEVANT_VECTOR[:] for _ in contents]

    with patch("src.rag.retrieve.embed_chunks", new=fake_embed):
        results = await retrieve_context(
            chatbot_id=chatbot_a_id,
            query="refund policy",
        )

    returned_ids = {r.id for r in results}
    assert chunk_b_id not in returned_ids
    # Chatbot A's chunk should be there
    assert chunk_a_id in returned_ids


@pytest.mark.asyncio
async def test_retrieve_excludes_soft_deleted_documents():
    """Chunks whose document is soft-deleted are never returned."""
    chatbot_id, chunk_id = await _create_chatbot_with_chunk(doc_deleted=True)

    async def fake_embed(contents):
        return [RELEVANT_VECTOR[:] for _ in contents]

    with patch("src.rag.retrieve.embed_chunks", new=fake_embed):
        results = await retrieve_context(
            chatbot_id=chatbot_id,
            query="how do I get a refund",
        )

    assert results == []


@pytest.mark.asyncio
async def test_retrieve_returns_dataclass_fields():
    """Returned RetrievedChunk has all required fields populated."""
    chatbot_id, chunk_id = await _create_chatbot_with_chunk()

    async def fake_embed(contents):
        return [RELEVANT_VECTOR[:] for _ in contents]

    with patch("src.rag.retrieve.embed_chunks", new=fake_embed):
        results = await retrieve_context(
            chatbot_id=chatbot_id,
            query="refund",
        )

    assert len(results) >= 1
    r = results[0]
    assert r.id == chunk_id
    assert isinstance(r.document_id, str)
    assert r.document_name == "refund_policy.txt"
    assert "refund" in r.content.lower()
    assert 0.75 < r.similarity <= 1.0


@pytest.mark.asyncio
async def test_retrieve_respects_top_k():
    """top_k limits the candidate set before Python similarity filter."""
    async with async_session_factory() as db:
        tenant = Tenant(
            name="TopK Tenant",
            plan="free",
            clerk_user_id=f"topk_{uuid.uuid4().hex[:12]}",
        )
        db.add(tenant)
        await db.flush()

        chatbot = Chatbot(tenant_id=tenant.id, name="TopK Bot")
        db.add(chatbot)
        await db.flush()

        doc = Document(
            chatbot_id=chatbot.id,
            tenant_id=tenant.id,
            filename="topk.txt",
            mime_type="text/plain",
            s3_key=f"test/{uuid.uuid4().hex}/topk.txt",
            status="ready",
        )
        db.add(doc)
        await db.flush()

        for i in range(5):
            chunk = Chunk(
                document_id=doc.id,
                chatbot_id=chatbot.id,
                tenant_id=tenant.id,
                content=f"Chunk number {i} about the refund policy.",
                token_count=10,
                chunk_index=i,
                embedding=RELEVANT_VECTOR[:],
                embedding_model="text-embedding-3-small",
            )
            db.add(chunk)

        await db.commit()
        chatbot_id = str(chatbot.id)

    async def fake_embed(contents):
        return [RELEVANT_VECTOR[:] for _ in contents]

    with patch("src.rag.retrieve.embed_chunks", new=fake_embed):
        results_k2 = await retrieve_context(
            chatbot_id=chatbot_id,
            query="refund",
            top_k=2,
        )
        results_k5 = await retrieve_context(
            chatbot_id=chatbot_id,
            query="refund",
            top_k=5,
        )

    assert len(results_k2) == 2
    assert len(results_k5) == 5


# --------------------------------------------------------------------------------------
# Slice 12 — Hybrid search (BM25 + vector) tests
# --------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_retrieve_keyword_only_hit():
    """
    Keyword-only AC: chunk containing 'SKU-8821' is found via FTS even when the
    query vector is anti-parallel (cosine similarity = -1.0, well below min_similarity).
    The returned chunk must have similarity == 0.0 (keyword hit, not vector hit).
    """
    chatbot_id, chunk_id = await _create_chatbot_with_chunk(
        content="SKU-8821 warranty terms and conditions apply",
        embedding=RELEVANT_VECTOR[:],  # chunk stored with RELEVANT_VECTOR
    )

    # Query embedding is OFFTOPIC_VECTOR → cosine similarity to RELEVANT_VECTOR = -1.0
    async def fake_embed_offtopic(contents):
        return [OFFTOPIC_VECTOR[:] for _ in contents]

    with patch("src.rag.retrieve.embed_chunks", new=fake_embed_offtopic):
        results = await retrieve_context(
            chatbot_id=chatbot_id,
            query="SKU-8821",
        )

    assert len(results) >= 1, "Keyword-only hit should be returned via FTS"
    matching = [r for r in results if r.id == chunk_id]
    assert len(matching) == 1
    assert matching[0].similarity == 0.0  # keyword-only hit carries 0.0 similarity


@pytest.mark.asyncio
async def test_retrieve_keyword_hit_with_regular_word():
    """
    FTS hit using a regular English keyword: chunk with 'warranty' is returned
    via keyword match even when vector similarity is below threshold.
    """
    chatbot_id, chunk_id = await _create_chatbot_with_chunk(
        content="Full warranty coverage is provided for all products",
        embedding=RELEVANT_VECTOR[:],
    )

    async def fake_embed_offtopic(contents):
        return [OFFTOPIC_VECTOR[:] for _ in contents]

    with patch("src.rag.retrieve.embed_chunks", new=fake_embed_offtopic):
        results = await retrieve_context(
            chatbot_id=chatbot_id,
            query="warranty coverage",
        )

    assert len(results) >= 1
    matching = [r for r in results if r.id == chunk_id]
    assert len(matching) == 1
    assert matching[0].similarity == 0.0


@pytest.mark.asyncio
async def test_retrieve_hybrid_chunk_scores_higher_when_both_match():
    """
    A chunk that appears in both vector AND FTS results gets a higher RRF score
    than a chunk that appears in only one, and should rank first.
    """
    async with async_session_factory() as db:
        tenant = Tenant(
            name="Hybrid RRF Tenant",
            plan="free",
            clerk_user_id=f"rrf_{uuid.uuid4().hex[:12]}",
        )
        db.add(tenant)
        await db.flush()

        chatbot = Chatbot(tenant_id=tenant.id, name="RRF Bot")
        db.add(chatbot)
        await db.flush()

        doc = Document(
            chatbot_id=chatbot.id,
            tenant_id=tenant.id,
            filename="rrf_test.txt",
            mime_type="text/plain",
            s3_key=f"test/{uuid.uuid4().hex}/rrf.txt",
            status="ready",
        )
        db.add(doc)
        await db.flush()

        # Chunk A: matches both vector (RELEVANT_VECTOR) and keyword "refund"
        chunk_a = Chunk(
            document_id=doc.id,
            chatbot_id=chatbot.id,
            tenant_id=tenant.id,
            content="Our refund policy allows returns within 30 days.",
            token_count=10,
            chunk_index=0,
            embedding=RELEVANT_VECTOR[:],
            embedding_model="text-embedding-3-small",
        )
        db.add(chunk_a)

        # Chunk B: matches FTS "refund policy" keyword but has OFFTOPIC_VECTOR (below threshold)
        chunk_b = Chunk(
            document_id=doc.id,
            chatbot_id=chatbot.id,
            tenant_id=tenant.id,
            content="Refund policy: all claims must be submitted within 30 days.",
            token_count=12,
            chunk_index=1,
            embedding=OFFTOPIC_VECTOR[:],
            embedding_model="text-embedding-3-small",
        )
        db.add(chunk_b)

        await db.commit()
        chatbot_id = str(chatbot.id)
        chunk_a_id = str(chunk_a.id)
        chunk_b_id = str(chunk_b.id)

    # Query embedding = RELEVANT_VECTOR → chunk_a has sim=1.0, chunk_b has sim=-1.0
    async def fake_embed(contents):
        return [RELEVANT_VECTOR[:] for _ in contents]

    with patch("src.rag.retrieve.embed_chunks", new=fake_embed):
        results = await retrieve_context(
            chatbot_id=chatbot_id,
            query="refund policy",
            top_k=5,
        )

    ids = [r.id for r in results]
    assert chunk_a_id in ids  # vector + keyword match → passes
    assert chunk_b_id in ids  # keyword-only match (similarity=0.0) → passes
    # chunk_a should rank first (both vector and FTS contribution → higher RRF)
    assert ids.index(chunk_a_id) < ids.index(chunk_b_id)


@pytest.mark.asyncio
async def test_retrieve_keyword_hit_respects_top_k():
    """top_k cap applies equally to hybrid results including keyword-only hits."""
    async with async_session_factory() as db:
        tenant = Tenant(
            name="KW TopK Tenant",
            plan="free",
            clerk_user_id=f"kwtopk_{uuid.uuid4().hex[:12]}",
        )
        db.add(tenant)
        await db.flush()

        chatbot = Chatbot(tenant_id=tenant.id, name="KW TopK Bot")
        db.add(chatbot)
        await db.flush()

        doc = Document(
            chatbot_id=chatbot.id,
            tenant_id=tenant.id,
            filename="kw_topk.txt",
            mime_type="text/plain",
            s3_key=f"test/{uuid.uuid4().hex}/kw_topk.txt",
            status="ready",
        )
        db.add(doc)
        await db.flush()

        # 5 chunks all with "warranty" keyword; all with OFFTOPIC_VECTOR (keyword-only hits)
        for i in range(5):
            chunk = Chunk(
                document_id=doc.id,
                chatbot_id=chatbot.id,
                tenant_id=tenant.id,
                content=f"Warranty clause number {i} covers all damages.",
                token_count=10,
                chunk_index=i,
                embedding=RELEVANT_VECTOR[:],
                embedding_model="text-embedding-3-small",
            )
            db.add(chunk)

        await db.commit()
        chatbot_id = str(chatbot.id)

    async def fake_embed_offtopic(contents):
        return [OFFTOPIC_VECTOR[:] for _ in contents]

    with patch("src.rag.retrieve.embed_chunks", new=fake_embed_offtopic):
        results_k2 = await retrieve_context(
            chatbot_id=chatbot_id,
            query="warranty",
            top_k=2,
        )
        results_k4 = await retrieve_context(
            chatbot_id=chatbot_id,
            query="warranty",
            top_k=4,
        )

    assert len(results_k2) == 2
    assert len(results_k4) == 4
    # All are keyword-only hits
    assert all(r.similarity == 0.0 for r in results_k2)
    assert all(r.similarity == 0.0 for r in results_k4)
