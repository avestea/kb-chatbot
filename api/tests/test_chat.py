"""Tests for Slice 8 — Chat Endpoint (SSE + LLM Streaming)."""
import json
import uuid
import pytest
import pytest_asyncio
from unittest.mock import patch, AsyncMock
from httpx import AsyncClient, ASGITransport

from src.main import app
from src.db.base import async_session_factory
from src.db.models import Tenant, Chatbot, Message, Conversation
from src.rag.retrieve import RetrievedChunk
from tests.fakes.anthropic import FakeStreamCompletion

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

OWNER = f"chat_owner_{uuid.uuid4().hex[:8]}"
HEADERS = {"Authorization": f"Bearer {OWNER}"}


def parse_sse(text: str) -> list[dict]:
    """Parse SSE text into list of {event, data} dicts."""
    events = []
    for block in text.strip().split("\n\n"):
        block = block.strip()
        if not block:
            continue
        event_type = None
        data = None
        for line in block.split("\n"):
            if line.startswith("event: "):
                event_type = line[7:].strip()
            elif line.startswith("data: "):
                data = json.loads(line[6:])
        if event_type and data is not None:
            events.append({"event": event_type, "data": data})
    return events


FAKE_CHUNKS = [
    RetrievedChunk(
        id=str(uuid.uuid4()),
        document_id=str(uuid.uuid4()),
        document_name="refund_policy.txt",
        content="Returns accepted within 30 days.",
        similarity=0.92,
    )
]

NO_CHUNKS: list[RetrievedChunk] = []

NO_ANSWER_TOKENS = ["I don't have information about that in my knowledge base."]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


@pytest_asyncio.fixture
async def chatbot_id(client):
    resp = await client.post(
        "/api/v1/chatbots",
        json={"name": "Test Chat Bot"},
        headers=HEADERS,
    )
    assert resp.status_code == 201
    return resp.json()["chatbot"]["id"]


# ---------------------------------------------------------------------------
# SSE event order and content
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sse_event_order(client, chatbot_id):
    """Events arrive in order: meta → token → done."""
    fake_llm = FakeStreamCompletion(tokens=["Hello", " world"])

    with patch("src.routes.chat.stream_completion", new=fake_llm), \
         patch("src.routes.chat.retrieve_context", new=AsyncMock(return_value=FAKE_CHUNKS)):
        resp = await client.post(
            f"/api/v1/chat/{chatbot_id}/message",
            json={"message": "What is the refund policy?", "session_id": "sess-1"},
        )

    assert resp.status_code == 200
    events = parse_sse(resp.text)
    event_types = [e["event"] for e in events]
    assert event_types[0] == "meta"
    assert "token" in event_types
    assert event_types[-1] == "done"


@pytest.mark.asyncio
async def test_sse_meta_has_conversation_id_and_source_count(client, chatbot_id):
    """meta event contains conversation_id and source_count."""
    fake_llm = FakeStreamCompletion(tokens=["Hi"])

    with patch("src.routes.chat.stream_completion", new=fake_llm), \
         patch("src.routes.chat.retrieve_context", new=AsyncMock(return_value=FAKE_CHUNKS)):
        resp = await client.post(
            f"/api/v1/chat/{chatbot_id}/message",
            json={"message": "refund?", "session_id": "sess-meta"},
        )

    events = parse_sse(resp.text)
    meta = next(e for e in events if e["event"] == "meta")
    assert "conversation_id" in meta["data"]
    assert meta["data"]["source_count"] == len(FAKE_CHUNKS)


@pytest.mark.asyncio
async def test_sse_token_events_contain_text(client, chatbot_id):
    """token events carry the streamed text fragments."""
    fake_llm = FakeStreamCompletion(tokens=["Hello", " world"])

    with patch("src.routes.chat.stream_completion", new=fake_llm), \
         patch("src.routes.chat.retrieve_context", new=AsyncMock(return_value=FAKE_CHUNKS)):
        resp = await client.post(
            f"/api/v1/chat/{chatbot_id}/message",
            json={"message": "hi", "session_id": "sess-tok"},
        )

    events = parse_sse(resp.text)
    token_texts = [e["data"]["text"] for e in events if e["event"] == "token"]
    assert token_texts == ["Hello", " world"]


@pytest.mark.asyncio
async def test_sse_done_has_message_id(client, chatbot_id):
    """done event contains message_id as a valid UUID string."""
    fake_llm = FakeStreamCompletion(tokens=["Done"])

    with patch("src.routes.chat.stream_completion", new=fake_llm), \
         patch("src.routes.chat.retrieve_context", new=AsyncMock(return_value=NO_CHUNKS)):
        resp = await client.post(
            f"/api/v1/chat/{chatbot_id}/message",
            json={"message": "hey", "session_id": "sess-done"},
        )

    events = parse_sse(resp.text)
    done = next(e for e in events if e["event"] == "done")
    assert "message_id" in done["data"]
    uuid.UUID(done["data"]["message_id"])  # must be valid UUID


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_assistant_message_persisted_with_tokens_and_chunks(client, chatbot_id):
    """After chat, assistant Message row has tokens_used and source_chunk_ids."""
    fake_llm = FakeStreamCompletion(tokens=["Answer"], input_tokens=15, output_tokens=5)

    with patch("src.routes.chat.stream_completion", new=fake_llm), \
         patch("src.routes.chat.retrieve_context", new=AsyncMock(return_value=FAKE_CHUNKS)):
        resp = await client.post(
            f"/api/v1/chat/{chatbot_id}/message",
            json={"message": "test persist", "session_id": "sess-persist"},
        )

    assert resp.status_code == 200
    events = parse_sse(resp.text)
    done = next(e for e in events if e["event"] == "done")
    msg_id = done["data"]["message_id"]

    async with async_session_factory() as db:
        from sqlalchemy import select as sa_select
        msg = (await db.execute(
            sa_select(Message).where(Message.id == uuid.UUID(msg_id))
        )).scalar_one()

    assert msg.role == "assistant"
    assert msg.content == "Answer"
    assert msg.tokens_used == 20  # 15 + 5
    assert msg.source_chunk_ids == [FAKE_CHUNKS[0].id]
    assert msg.prompt_version == "v1"
    assert msg.no_answer is False


@pytest.mark.asyncio
async def test_no_answer_flag_set_when_fallback_phrase_present(client, chatbot_id):
    """no_answer=True when response contains the fallback phrase."""
    fake_llm = FakeStreamCompletion(tokens=NO_ANSWER_TOKENS)

    with patch("src.routes.chat.stream_completion", new=fake_llm), \
         patch("src.routes.chat.retrieve_context", new=AsyncMock(return_value=NO_CHUNKS)):
        resp = await client.post(
            f"/api/v1/chat/{chatbot_id}/message",
            json={"message": "what is the airspeed velocity?", "session_id": "sess-noanswer"},
        )

    events = parse_sse(resp.text)
    done = next(e for e in events if e["event"] == "done")
    msg_id = done["data"]["message_id"]

    async with async_session_factory() as db:
        from sqlalchemy import select as sa_select
        msg = (await db.execute(
            sa_select(Message).where(Message.id == uuid.UUID(msg_id))
        )).scalar_one()

    assert msg.no_answer is True


@pytest.mark.asyncio
async def test_user_message_also_persisted(client, chatbot_id):
    """User's message is persisted before streaming begins."""
    fake_llm = FakeStreamCompletion(tokens=["OK"])

    with patch("src.routes.chat.stream_completion", new=fake_llm), \
         patch("src.routes.chat.retrieve_context", new=AsyncMock(return_value=NO_CHUNKS)):
        resp = await client.post(
            f"/api/v1/chat/{chatbot_id}/message",
            json={"message": "user input here", "session_id": "sess-usermsg"},
        )

    events = parse_sse(resp.text)
    meta = next(e for e in events if e["event"] == "meta")
    conv_id = meta["data"]["conversation_id"]

    async with async_session_factory() as db:
        from sqlalchemy import select as sa_select
        msgs = (await db.execute(
            sa_select(Message)
            .join(Conversation)
            .where(Conversation.id == uuid.UUID(conv_id), Message.role == "user")
        )).scalars().all()

    assert len(msgs) == 1
    assert msgs[0].content == "user input here"


# ---------------------------------------------------------------------------
# Conversation upsert
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_same_session_id_returns_same_conversation(client, chatbot_id):
    """Sending two messages with the same session_id reuses the conversation."""
    fake_llm = FakeStreamCompletion(tokens=["Hi"])

    with patch("src.routes.chat.stream_completion", new=fake_llm), \
         patch("src.routes.chat.retrieve_context", new=AsyncMock(return_value=NO_CHUNKS)):
        r1 = await client.post(
            f"/api/v1/chat/{chatbot_id}/message",
            json={"message": "first", "session_id": "shared-sess"},
        )
        r2 = await client.post(
            f"/api/v1/chat/{chatbot_id}/message",
            json={"message": "second", "session_id": "shared-sess"},
        )

    events1 = parse_sse(r1.text)
    events2 = parse_sse(r2.text)
    conv_id_1 = next(e for e in events1 if e["event"] == "meta")["data"]["conversation_id"]
    conv_id_2 = next(e for e in events2 if e["event"] == "meta")["data"]["conversation_id"]
    assert conv_id_1 == conv_id_2


@pytest.mark.asyncio
async def test_different_session_ids_get_different_conversations(client, chatbot_id):
    """Different session_ids produce different conversation rows."""
    fake_llm = FakeStreamCompletion(tokens=["Hi"])

    with patch("src.routes.chat.stream_completion", new=fake_llm), \
         patch("src.routes.chat.retrieve_context", new=AsyncMock(return_value=NO_CHUNKS)):
        r1 = await client.post(
            f"/api/v1/chat/{chatbot_id}/message",
            json={"message": "hello", "session_id": "sess-a"},
        )
        r2 = await client.post(
            f"/api/v1/chat/{chatbot_id}/message",
            json={"message": "hello", "session_id": "sess-b"},
        )

    events1 = parse_sse(r1.text)
    events2 = parse_sse(r2.text)
    conv_id_1 = next(e for e in events1 if e["event"] == "meta")["data"]["conversation_id"]
    conv_id_2 = next(e for e in events2 if e["event"] == "meta")["data"]["conversation_id"]
    assert conv_id_1 != conv_id_2


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_deleted_chatbot_returns_error_event(client):
    """Soft-deleted chatbot returns an error SSE event (no crash)."""
    # Create a chatbot then soft-delete it
    resp = await client.post(
        "/api/v1/chatbots",
        json={"name": "To Delete"},
        headers=HEADERS,
    )
    cb_id = resp.json()["chatbot"]["id"]
    await client.delete(f"/api/v1/chatbots/{cb_id}", headers=HEADERS)

    resp = await client.post(
        f"/api/v1/chat/{cb_id}/message",
        json={"message": "hello", "session_id": "sess-del"},
    )

    assert resp.status_code == 200
    events = parse_sse(resp.text)
    assert any(e["event"] == "error" for e in events)
    error_event = next(e for e in events if e["event"] == "error")
    assert "Chatbot not found" in error_event["data"]["message"]


@pytest.mark.asyncio
async def test_unknown_chatbot_id_returns_error_event(client):
    """Random chatbot_id that doesn't exist returns an error SSE event."""
    fake_id = str(uuid.uuid4())

    resp = await client.post(
        f"/api/v1/chat/{fake_id}/message",
        json={"message": "hello", "session_id": "sess-unknown"},
    )

    assert resp.status_code == 200
    events = parse_sse(resp.text)
    assert any(e["event"] == "error" for e in events)


@pytest.mark.asyncio
async def test_message_too_short_returns_422(client, chatbot_id):
    """Empty message fails Pydantic validation (min_length=1)."""
    resp = await client.post(
        f"/api/v1/chat/{chatbot_id}/message",
        json={"message": "", "session_id": "sess-short"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_missing_session_id_returns_422(client, chatbot_id):
    """Missing session_id fails Pydantic validation."""
    resp = await client.post(
        f"/api/v1/chat/{chatbot_id}/message",
        json={"message": "hello"},
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Prompt assembler unit tests
# ---------------------------------------------------------------------------

def test_build_system_prompt_with_chunks():
    from src.rag.prompt import build_system_prompt
    chunks = [
        RetrievedChunk(id="1", document_id="d1", document_name="doc.txt",
                       content="Returns in 30 days.", similarity=0.9),
    ]
    prompt = build_system_prompt("My Bot", chunks)
    assert "My Bot" in prompt
    assert "doc.txt" in prompt
    assert "Returns in 30 days." in prompt
    assert "(no relevant context found)" not in prompt


def test_build_system_prompt_without_chunks():
    from src.rag.prompt import build_system_prompt
    prompt = build_system_prompt("My Bot", [])
    assert "(no relevant context found)" in prompt
    assert "My Bot" in prompt


def test_prompt_version_constant():
    from src.rag.prompt import PROMPT_VERSION
    assert PROMPT_VERSION == "v1"


# ---------------------------------------------------------------------------
# Slice 10 — Explainability: sources SSE event + source_chunks persistence
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sources_event_present_when_chunks_exist(client, chatbot_id):
    """sources event is emitted when retrieved chunks exist."""
    fake_llm = FakeStreamCompletion(tokens=["Answer"])

    with patch("src.routes.chat.stream_completion", new=fake_llm), \
         patch("src.routes.chat.retrieve_context", new=AsyncMock(return_value=FAKE_CHUNKS)):
        resp = await client.post(
            f"/api/v1/chat/{chatbot_id}/message",
            json={"message": "refund policy?", "session_id": "sess-src-1"},
        )

    events = parse_sse(resp.text)
    event_types = [e["event"] for e in events]
    assert "sources" in event_types


@pytest.mark.asyncio
async def test_sources_event_before_token_events(client, chatbot_id):
    """sources event appears before the first token event."""
    fake_llm = FakeStreamCompletion(tokens=["Hello"])

    with patch("src.routes.chat.stream_completion", new=fake_llm), \
         patch("src.routes.chat.retrieve_context", new=AsyncMock(return_value=FAKE_CHUNKS)):
        resp = await client.post(
            f"/api/v1/chat/{chatbot_id}/message",
            json={"message": "refund?", "session_id": "sess-src-order"},
        )

    events = parse_sse(resp.text)
    event_types = [e["event"] for e in events]
    sources_idx = event_types.index("sources")
    token_idx = event_types.index("token")
    assert sources_idx < token_idx


@pytest.mark.asyncio
async def test_sources_event_payload_structure(client, chatbot_id):
    """sources event payload has index, chunk_id, document_name, snippet, similarity."""
    fake_llm = FakeStreamCompletion(tokens=["OK"])

    with patch("src.routes.chat.stream_completion", new=fake_llm), \
         patch("src.routes.chat.retrieve_context", new=AsyncMock(return_value=FAKE_CHUNKS)):
        resp = await client.post(
            f"/api/v1/chat/{chatbot_id}/message",
            json={"message": "refund?", "session_id": "sess-src-payload"},
        )

    events = parse_sse(resp.text)
    src_event = next(e for e in events if e["event"] == "sources")
    sources = src_event["data"]["sources"]
    assert len(sources) == 1
    s = sources[0]
    assert s["index"] == 1
    assert "chunk_id" in s
    assert s["document_name"] == FAKE_CHUNKS[0].document_name
    assert s["snippet"] == FAKE_CHUNKS[0].content[:300]
    assert 0.0 <= s["similarity"] <= 1.0


@pytest.mark.asyncio
async def test_no_sources_event_when_no_chunks(client, chatbot_id):
    """sources event is NOT emitted when no chunks are retrieved."""
    fake_llm = FakeStreamCompletion(tokens=["I don't have information about that in my knowledge base."])

    with patch("src.routes.chat.stream_completion", new=fake_llm), \
         patch("src.routes.chat.retrieve_context", new=AsyncMock(return_value=NO_CHUNKS)):
        resp = await client.post(
            f"/api/v1/chat/{chatbot_id}/message",
            json={"message": "what is the airspeed velocity?", "session_id": "sess-nosrc"},
        )

    events = parse_sse(resp.text)
    assert not any(e["event"] == "sources" for e in events)


@pytest.mark.asyncio
async def test_source_chunks_persisted_on_message(client, chatbot_id):
    """source_chunks JSONB is populated on the assistant message."""
    fake_llm = FakeStreamCompletion(tokens=["Answer"], input_tokens=10, output_tokens=5)

    with patch("src.routes.chat.stream_completion", new=fake_llm), \
         patch("src.routes.chat.retrieve_context", new=AsyncMock(return_value=FAKE_CHUNKS)):
        resp = await client.post(
            f"/api/v1/chat/{chatbot_id}/message",
            json={"message": "refund?", "session_id": "sess-src-persist"},
        )

    events = parse_sse(resp.text)
    done = next(e for e in events if e["event"] == "done")
    msg_id = done["data"]["message_id"]

    async with async_session_factory() as db:
        from sqlalchemy import select as sa_select
        msg = (await db.execute(
            sa_select(Message).where(Message.id == uuid.UUID(msg_id))
        )).scalar_one()

    assert msg.source_chunks is not None
    assert len(msg.source_chunks) == 1
    s = msg.source_chunks[0]
    assert s["index"] == 1
    assert s["document_name"] == FAKE_CHUNKS[0].document_name
    assert 0.0 <= s["similarity"] <= 1.0


@pytest.mark.asyncio
async def test_source_chunks_null_when_no_chunks(client, chatbot_id):
    """source_chunks is NULL on the message when no context chunks exist."""
    fake_llm = FakeStreamCompletion(tokens=["I don't have information about that in my knowledge base."])

    with patch("src.routes.chat.stream_completion", new=fake_llm), \
         patch("src.routes.chat.retrieve_context", new=AsyncMock(return_value=NO_CHUNKS)):
        resp = await client.post(
            f"/api/v1/chat/{chatbot_id}/message",
            json={"message": "nobody knows", "session_id": "sess-src-null"},
        )

    events = parse_sse(resp.text)
    done = next(e for e in events if e["event"] == "done")
    msg_id = done["data"]["message_id"]

    async with async_session_factory() as db:
        from sqlalchemy import select as sa_select
        msg = (await db.execute(
            sa_select(Message).where(Message.id == uuid.UUID(msg_id))
        )).scalar_one()

    assert msg.source_chunks is None


@pytest.mark.asyncio
async def test_similarity_scores_in_valid_range(client, chatbot_id):
    """All similarity scores in sources payload are between 0 and 1."""
    multi_chunks = [
        RetrievedChunk(id=str(uuid.uuid4()), document_id=str(uuid.uuid4()),
                       document_name="doc1.pdf", content="chunk one", similarity=0.92),
        RetrievedChunk(id=str(uuid.uuid4()), document_id=str(uuid.uuid4()),
                       document_name="doc2.pdf", content="chunk two", similarity=0.78),
    ]
    fake_llm = FakeStreamCompletion(tokens=["OK"])

    with patch("src.routes.chat.stream_completion", new=fake_llm), \
         patch("src.routes.chat.retrieve_context", new=AsyncMock(return_value=multi_chunks)):
        resp = await client.post(
            f"/api/v1/chat/{chatbot_id}/message",
            json={"message": "test?", "session_id": "sess-sim-range"},
        )

    events = parse_sse(resp.text)
    src_event = next(e for e in events if e["event"] == "sources")
    for s in src_event["data"]["sources"]:
        assert 0.0 <= s["similarity"] <= 1.0
