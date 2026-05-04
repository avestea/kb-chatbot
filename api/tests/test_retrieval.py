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
