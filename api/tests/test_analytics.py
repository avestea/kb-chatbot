"""Tests for Slice 11 — Evaluation Dashboard (analytics routes)."""
import uuid
import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy import select

from src.main import app
from src.db.base import async_session_factory
from src.db.models import Chatbot, Conversation, Message

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

OWNER = f"analytics_owner_{uuid.uuid4().hex[:8]}"
OTHER = f"analytics_other_{uuid.uuid4().hex[:8]}"
HEADERS = {"Authorization": f"Bearer {OWNER}"}
OTHER_HEADERS = {"Authorization": f"Bearer {OTHER}"}


@pytest_asyncio.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


@pytest_asyncio.fixture
async def chatbot_id(client):
    resp = await client.post(
        "/api/v1/chatbots",
        json={"name": "Analytics Test Bot"},
        headers=HEADERS,
    )
    assert resp.status_code == 201
    return resp.json()["chatbot"]["id"]


@pytest_asyncio.fixture
async def seeded(client, chatbot_id):
    """Seed two conversations:
    - conv1: normal answer with source_chunks (similarity=0.92)
    - conv2: no-answer response, no source_chunks
    """
    async with async_session_factory() as session:
        chatbot = (await session.execute(
            select(Chatbot).where(Chatbot.id == uuid.UUID(chatbot_id))
        )).scalar_one()

        conv1 = Conversation(chatbot_id=chatbot.id, session_id=f"s1_{uuid.uuid4().hex}")
        conv2 = Conversation(chatbot_id=chatbot.id, session_id=f"s2_{uuid.uuid4().hex}")
        session.add_all([conv1, conv2])
        await session.flush()

        session.add_all([
            Message(
                conversation_id=conv1.id,
                role="user",
                content="What is the refund policy?",
                no_answer=False,
            ),
            Message(
                conversation_id=conv1.id,
                role="assistant",
                content="You can return items within 30 days.",
                no_answer=False,
                source_chunks=[
                    {
                        "index": 0,
                        "chunk_id": str(uuid.uuid4()),
                        "document_name": "policy.txt",
                        "snippet": "Returns accepted within 30 days.",
                        "similarity": 0.92,
                    }
                ],
                tokens_used=150,
                prompt_version="v1",
            ),
            Message(
                conversation_id=conv2.id,
                role="user",
                content="What is the weather today?",
                no_answer=False,
            ),
            Message(
                conversation_id=conv2.id,
                role="assistant",
                content="I don't have information about that in my knowledge base.",
                no_answer=True,
                source_chunks=None,
                tokens_used=50,
                prompt_version="v1",
            ),
        ])
        await session.commit()

    return {
        "chatbot_id": chatbot_id,
        "conv1_id": str(conv1.id),
        "conv2_id": str(conv2.id),
    }


# ---------------------------------------------------------------------------
# Summary endpoint
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_summary_empty(client, chatbot_id):
    """Summary with no conversations returns all zeros."""
    resp = await client.get("/api/v1/analytics/summary", headers=HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_conversations"] == 0
    assert data["total_messages"] == 0
    assert data["no_answer_rate"] == 0.0
    assert data["avg_top_similarity"] is None


@pytest.mark.asyncio
async def test_summary_counts(client, seeded):
    """Summary returns correct conversation and message counts."""
    resp = await client.get("/api/v1/analytics/summary", headers=HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_conversations"] >= 2
    assert data["total_messages"] >= 2  # assistant messages


@pytest.mark.asyncio
async def test_summary_no_answer_rate(client, seeded):
    """no_answer_rate reflects actual no_answer=True flags (1 out of 2 assistant msgs = 0.5)."""
    resp = await client.get("/api/v1/analytics/summary", headers=HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["no_answer_count"] >= 1
    assert 0 < data["no_answer_rate"] <= 1.0


@pytest.mark.asyncio
async def test_summary_avg_similarity(client, seeded):
    """avg_top_similarity is computed from source_chunks when present."""
    resp = await client.get("/api/v1/analytics/summary", headers=HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    # Only conv1 has source_chunks with similarity=0.92
    assert data["avg_top_similarity"] is not None
    assert 0 < data["avg_top_similarity"] <= 1.0


@pytest.mark.asyncio
async def test_summary_filtered_by_chatbot(client, seeded):
    """Summary can be filtered to a specific chatbot."""
    chatbot_id = seeded["chatbot_id"]
    resp = await client.get(
        f"/api/v1/analytics/summary?chatbot_id={chatbot_id}",
        headers=HEADERS,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_conversations"] >= 2


@pytest.mark.asyncio
async def test_summary_requires_auth(client):
    resp = await client.get("/api/v1/analytics/summary")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Conversations list endpoint
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_conversations(client, seeded):
    """Lists conversations with first_question and has_failure."""
    resp = await client.get("/api/v1/analytics/conversations", headers=HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 2
    items = data["items"]
    assert len(items) >= 2

    ids = [item["id"] for item in items]
    assert seeded["conv1_id"] in ids or any(i == seeded["conv1_id"] for i in ids)

    # Find our seeded conversations
    conv1 = next((i for i in items if str(i["id"]) == seeded["conv1_id"]), None)
    conv2 = next((i for i in items if str(i["id"]) == seeded["conv2_id"]), None)

    assert conv1 is not None
    assert conv2 is not None
    assert conv1["has_failure"] is False or conv1["has_failure"] == False
    assert conv2["has_failure"] is True or conv2["has_failure"] == True
    assert "What is the refund policy" in (conv1.get("first_question") or "")


@pytest.mark.asyncio
async def test_list_conversations_no_answer_only(client, seeded):
    """no_answer_only=true returns only conversations with at least one failed answer."""
    resp = await client.get(
        "/api/v1/analytics/conversations?no_answer_only=true",
        headers=HEADERS,
    )
    assert resp.status_code == 200
    data = resp.json()
    items = data["items"]

    # All returned conversations must have has_failure=True
    for item in items:
        assert item["has_failure"] is True

    # conv2 should be included (has no_answer)
    ids = [str(item["id"]) for item in items]
    assert seeded["conv2_id"] in ids

    # conv1 should NOT be included (no no_answer)
    assert seeded["conv1_id"] not in ids


@pytest.mark.asyncio
async def test_list_conversations_pagination(client, seeded):
    """Pagination params are respected."""
    resp = await client.get(
        "/api/v1/analytics/conversations?limit=1&offset=0",
        headers=HEADERS,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["items"]) == 1
    assert data["has_more"] is True


@pytest.mark.asyncio
async def test_list_conversations_requires_auth(client):
    resp = await client.get("/api/v1/analytics/conversations")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Conversation messages endpoint
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_conversation_messages(client, seeded):
    """Returns full message thread with source_chunks on assistant turns."""
    conv1_id = seeded["conv1_id"]
    resp = await client.get(
        f"/api/v1/analytics/conversations/{conv1_id}/messages",
        headers=HEADERS,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["conversation_id"] == conv1_id
    messages = data["messages"]
    assert len(messages) == 2

    user_msg = next(m for m in messages if m["role"] == "user")
    asst_msg = next(m for m in messages if m["role"] == "assistant")

    assert "refund" in user_msg["content"].lower()
    assert asst_msg["no_answer"] is False
    assert len(asst_msg["source_chunks"]) == 1
    assert asst_msg["source_chunks"][0]["similarity"] == 0.92
    assert asst_msg["tokens_used"] == 150


@pytest.mark.asyncio
async def test_get_conversation_messages_no_answer(client, seeded):
    """No-answer conversation has empty source_chunks."""
    conv2_id = seeded["conv2_id"]
    resp = await client.get(
        f"/api/v1/analytics/conversations/{conv2_id}/messages",
        headers=HEADERS,
    )
    assert resp.status_code == 200
    data = resp.json()
    messages = data["messages"]
    asst_msg = next(m for m in messages if m["role"] == "assistant")
    assert asst_msg["no_answer"] is True
    assert asst_msg["source_chunks"] == []


@pytest.mark.asyncio
async def test_get_conversation_messages_cross_tenant(client, seeded):
    """A tenant cannot inspect conversations belonging to another tenant (404)."""
    conv1_id = seeded["conv1_id"]
    resp = await client.get(
        f"/api/v1/analytics/conversations/{conv1_id}/messages",
        headers=OTHER_HEADERS,
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_conversation_messages_not_found(client, chatbot_id):
    """Non-existent conversation returns 404."""
    fake_id = str(uuid.uuid4())
    resp = await client.get(
        f"/api/v1/analytics/conversations/{fake_id}/messages",
        headers=HEADERS,
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_conversation_messages_requires_auth(client, seeded):
    conv1_id = seeded["conv1_id"]
    resp = await client.get(f"/api/v1/analytics/conversations/{conv1_id}/messages")
    assert resp.status_code == 401
