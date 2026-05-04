"""Tests for Slice 14 — Thumbs Up / Down Feedback."""
import uuid
import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy import select

from src.main import app
from src.db.base import async_session_factory
from src.db.models import Chatbot, Conversation, Message, Feedback

OWNER = f"feedback_owner_{uuid.uuid4().hex[:8]}"
OTHER = f"feedback_other_{uuid.uuid4().hex[:8]}"
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
        json={"name": "Feedback Test Bot"},
        headers=HEADERS,
    )
    assert resp.status_code == 201
    return resp.json()["chatbot"]["id"]


@pytest_asyncio.fixture
async def assistant_message_id(chatbot_id):
    """Seed one conversation with an assistant message owned by OWNER."""
    async with async_session_factory() as session:
        conv = Conversation(chatbot_id=uuid.UUID(chatbot_id), session_id=f"s_{uuid.uuid4().hex}")
        session.add(conv)
        await session.flush()

        user_msg = Message(
            conversation_id=conv.id,
            role="user",
            content="What is the refund policy?",
            no_answer=False,
        )
        asst_msg = Message(
            conversation_id=conv.id,
            role="assistant",
            content="You can return items within 30 days.",
            no_answer=False,
            tokens_used=100,
            prompt_version="v1",
        )
        session.add_all([user_msg, asst_msg])
        await session.commit()

    return str(asst_msg.id)


# ---------------------------------------------------------------------------
# POST /api/v1/feedback — happy path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_submit_thumbs_up(client, assistant_message_id):
    resp = await client.post(
        "/api/v1/feedback",
        json={"message_id": assistant_message_id, "rating": 1},
        headers=HEADERS,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["message_id"] == assistant_message_id
    assert data["rating"] == 1


@pytest.mark.asyncio
async def test_submit_thumbs_down(client, assistant_message_id):
    resp = await client.post(
        "/api/v1/feedback",
        json={"message_id": assistant_message_id, "rating": -1},
        headers=HEADERS,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["rating"] == -1


@pytest.mark.asyncio
async def test_upsert_rating(client, assistant_message_id):
    """Submitting feedback twice updates the rating rather than creating a duplicate row."""
    await client.post(
        "/api/v1/feedback",
        json={"message_id": assistant_message_id, "rating": 1},
        headers=HEADERS,
    )
    resp = await client.post(
        "/api/v1/feedback",
        json={"message_id": assistant_message_id, "rating": -1},
        headers=HEADERS,
    )
    assert resp.status_code == 201

    async with async_session_factory() as session:
        rows = (await session.execute(
            select(Feedback).where(Feedback.message_id == uuid.UUID(assistant_message_id))
        )).scalars().all()
    assert len(rows) == 1
    assert rows[0].rating == -1


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rating_zero_rejected(client, assistant_message_id):
    resp = await client.post(
        "/api/v1/feedback",
        json={"message_id": assistant_message_id, "rating": 0},
        headers=HEADERS,
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_rating_out_of_range_rejected(client, assistant_message_id):
    resp = await client.post(
        "/api/v1/feedback",
        json={"message_id": assistant_message_id, "rating": 2},
        headers=HEADERS,
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Auth & tenant isolation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_feedback_requires_auth(client, assistant_message_id):
    resp = await client.post(
        "/api/v1/feedback",
        json={"message_id": assistant_message_id, "rating": 1},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_feedback_cross_tenant_404(client, assistant_message_id):
    """Another tenant cannot submit feedback on a message they don't own."""
    resp = await client.post(
        "/api/v1/feedback",
        json={"message_id": assistant_message_id, "rating": 1},
        headers=OTHER_HEADERS,
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_feedback_nonexistent_message(client, chatbot_id):
    resp = await client.post(
        "/api/v1/feedback",
        json={"message_id": str(uuid.uuid4()), "rating": 1},
        headers=HEADERS,
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Analytics summary includes feedback stats
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_summary_includes_feedback_fields(client, chatbot_id, assistant_message_id):
    """Summary always returns feedback fields even with zero feedback."""
    resp = await client.get("/api/v1/analytics/summary", headers=HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert "total_feedback" in data
    assert "thumbs_up" in data
    assert "thumbs_down" in data
    assert "satisfaction_rate" in data


@pytest.mark.asyncio
async def test_summary_feedback_counts(client, chatbot_id, assistant_message_id):
    """After submitting feedback, summary counts update correctly."""
    await client.post(
        "/api/v1/feedback",
        json={"message_id": assistant_message_id, "rating": 1},
        headers=HEADERS,
    )
    resp = await client.get("/api/v1/analytics/summary", headers=HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_feedback"] >= 1
    assert data["thumbs_up"] >= 1
    assert data["satisfaction_rate"] is not None
    assert 0 < data["satisfaction_rate"] <= 1.0


@pytest.mark.asyncio
async def test_summary_thumbs_down(client, chatbot_id, assistant_message_id):
    """Thumbs-down increments the correct counter."""
    await client.post(
        "/api/v1/feedback",
        json={"message_id": assistant_message_id, "rating": -1},
        headers=HEADERS,
    )
    resp = await client.get("/api/v1/analytics/summary", headers=HEADERS)
    data = resp.json()
    assert data["thumbs_down"] >= 1


# ---------------------------------------------------------------------------
# Conversation messages include rating
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_conversation_messages_rating_field(client, chatbot_id, assistant_message_id):
    """Messages endpoint always includes a 'rating' key (None before any feedback)."""
    async with async_session_factory() as session:
        msg = (await session.execute(
            select(Message).where(Message.id == uuid.UUID(assistant_message_id))
        )).scalar_one()
        conv_id = str(msg.conversation_id)

    resp = await client.get(
        f"/api/v1/analytics/conversations/{conv_id}/messages",
        headers=HEADERS,
    )
    assert resp.status_code == 200
    messages = resp.json()["messages"]
    asst = next(m for m in messages if m["role"] == "assistant")
    assert "rating" in asst
    assert asst["rating"] is None


@pytest.mark.asyncio
async def test_conversation_messages_rating_after_feedback(client, chatbot_id, assistant_message_id):
    """After submitting feedback the rating field reflects the stored value."""
    await client.post(
        "/api/v1/feedback",
        json={"message_id": assistant_message_id, "rating": 1},
        headers=HEADERS,
    )

    async with async_session_factory() as session:
        msg = (await session.execute(
            select(Message).where(Message.id == uuid.UUID(assistant_message_id))
        )).scalar_one()
        conv_id = str(msg.conversation_id)

    resp = await client.get(
        f"/api/v1/analytics/conversations/{conv_id}/messages",
        headers=HEADERS,
    )
    messages = resp.json()["messages"]
    asst = next(m for m in messages if m["role"] == "assistant")
    assert asst["rating"] == 1
