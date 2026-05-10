"""Tests for Slice 15 — Observability & Token Tracking."""
import uuid
from decimal import Decimal

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy import select

from src.main import app
from src.db.base import async_session_factory
from src.db.models import Chatbot, CostRate, ObservationLog
from src.lib.observability import RequestContext, log_request, seed_cost_rates
from src.config.observability import get_cost

OWNER = f"obs_owner_{uuid.uuid4().hex[:8]}"
OTHER = f"obs_other_{uuid.uuid4().hex[:8]}"
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
        json={"name": "Obs Test Bot"},
        headers=HEADERS,
    )
    assert resp.status_code == 201
    return resp.json()["chatbot"]["id"]


@pytest_asyncio.fixture
async def tenant_id(chatbot_id):
    async with async_session_factory() as session:
        bot = (await session.execute(
            select(Chatbot).where(Chatbot.id == uuid.UUID(chatbot_id))
        )).scalar_one()
        return str(bot.tenant_id)


async def _seed(session, *, tenant_id, chatbot_id=None, provider="anthropic",
                model="claude-sonnet-4-6", phase="chat_response", direction="input",
                tokens=1000, latency_ms=200, chunk_count=0, ctx=None):
    """Helper: add one ObservationLog row and commit."""
    await log_request(
        session,
        tenant_id=tenant_id,
        chatbot_id=chatbot_id,
        provider=provider,
        model=model,
        phase=phase,
        direction=direction,
        tokens=tokens,
        latency_ms=latency_ms,
        chunk_count=chunk_count,
        request_context=ctx,
    )
    await session.commit()


# ---------------------------------------------------------------------------
# Unit — cost calculation
# ---------------------------------------------------------------------------

def test_get_cost_sonnet_input():
    cost = get_cost("anthropic", "claude-sonnet-4-6", "input", 1_000_000)
    assert cost == Decimal("3.00")


def test_get_cost_sonnet_output():
    cost = get_cost("anthropic", "claude-sonnet-4-6", "output", 1_000_000)
    assert cost == Decimal("15.00")


def test_get_cost_embedding():
    cost = get_cost("openai", "text-embedding-3-small", "input", 1_000_000)
    assert cost == Decimal("0.02")


def test_get_cost_fractional():
    # 500 tokens of Sonnet input = 3.00 / 1M * 500 = 0.0000015 USD
    cost = get_cost("anthropic", "claude-sonnet-4-6", "input", 500)
    assert cost == Decimal("500") / Decimal("1000000") * Decimal("3.00")


# ---------------------------------------------------------------------------
# Unit — seed_cost_rates idempotency
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_seed_cost_rates_idempotent():
    """seed_cost_rates() is a no-op when rows already exist."""
    async with async_session_factory() as session:
        await seed_cost_rates(session)
        count_before = (await session.execute(
            select(CostRate)
        )).scalars().all()

    async with async_session_factory() as session:
        await seed_cost_rates(session)
        count_after = (await session.execute(
            select(CostRate)
        )).scalars().all()

    assert len(count_before) == len(count_after)
    assert len(count_after) > 0


# ---------------------------------------------------------------------------
# Unit — log_request persists correct cost
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_log_request_persists_row(chatbot_id, tenant_id):
    ctx = RequestContext.new()
    async with async_session_factory() as session:
        returned_id = await log_request(
            session,
            tenant_id=tenant_id,
            chatbot_id=chatbot_id,
            provider="anthropic",
            model="claude-sonnet-4-6",
            phase="chat_response",
            direction="input",
            tokens=2000,
            latency_ms=350,
            request_context=ctx,
        )
        await session.commit()

    assert returned_id == ctx.request_id

    async with async_session_factory() as session:
        row = (await session.execute(
            select(ObservationLog).where(ObservationLog.request_id == ctx.request_id)
        )).scalar_one()

    assert row.tokens == 2000
    assert row.latency_ms == 350
    assert row.phase == "chat_response"
    assert row.direction == "input"
    expected_cost = get_cost("anthropic", "claude-sonnet-4-6", "input", 2000)
    assert row.cost_usd == expected_cost


# ---------------------------------------------------------------------------
# GET /api/v1/observability/summary
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_summary_returns_totals(client, chatbot_id, tenant_id):
    async with async_session_factory() as session:
        await _seed(session, tenant_id=tenant_id, chatbot_id=chatbot_id, tokens=500, latency_ms=100)
        await _seed(session, tenant_id=tenant_id, chatbot_id=chatbot_id, tokens=300, latency_ms=200)

    resp = await client.get("/api/v1/observability/summary", headers=HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_tokens"] >= 800
    assert data["total_requests"] >= 2
    assert data["total_cost_usd"] >= 0
    assert data["avg_cost_per_request"] >= 0
    assert "avg_latency_ms" in data


@pytest.mark.asyncio
async def test_summary_filtered_by_chatbot(client, chatbot_id, tenant_id):
    async with async_session_factory() as session:
        await _seed(session, tenant_id=tenant_id, chatbot_id=chatbot_id, tokens=1000)

    resp = await client.get(
        "/api/v1/observability/summary",
        params={"chatbot_id": chatbot_id},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_tokens"] >= 1000
    assert data["total_requests"] >= 1


@pytest.mark.asyncio
async def test_summary_foreign_chatbot_returns_zeros(client, tenant_id):
    """Passing another tenant's chatbot_id returns zeros, not an error."""
    resp = await client.get(
        "/api/v1/observability/summary",
        params={"chatbot_id": str(uuid.uuid4())},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_cost_usd"] == 0.0
    assert data["total_tokens"] == 0
    assert data["total_requests"] == 0


# ---------------------------------------------------------------------------
# GET /api/v1/observability/breakdown/by-chatbot
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_breakdown_by_chatbot(client, chatbot_id, tenant_id):
    async with async_session_factory() as session:
        await _seed(session, tenant_id=tenant_id, chatbot_id=chatbot_id, tokens=400)

    resp = await client.get("/api/v1/observability/breakdown/by-chatbot", headers=HEADERS)
    assert resp.status_code == 200
    rows = resp.json()
    assert isinstance(rows, list)
    ids = [r["chatbot_id"] for r in rows]
    assert chatbot_id in ids
    row = next(r for r in rows if r["chatbot_id"] == chatbot_id)
    assert row["total_tokens"] >= 400
    assert "chatbot_name" in row
    assert "total_cost_usd" in row
    assert "total_requests" in row


# ---------------------------------------------------------------------------
# GET /api/v1/observability/breakdown/by-phase
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_breakdown_by_phase(client, chatbot_id, tenant_id):
    async with async_session_factory() as session:
        await _seed(session, tenant_id=tenant_id, chatbot_id=chatbot_id, phase="chat_response", tokens=600)
        await _seed(session, tenant_id=tenant_id, chatbot_id=None,
                    provider="openai", model="text-embedding-3-small",
                    phase="embed_query", direction="input", tokens=50)

    resp = await client.get("/api/v1/observability/breakdown/by-phase", headers=HEADERS)
    assert resp.status_code == 200
    rows = resp.json()
    phases = [r["phase"] for r in rows]
    assert "chat_response" in phases
    assert "embed_query" in phases
    for row in rows:
        assert "total_cost_usd" in row
        assert "avg_latency_ms" in row


# ---------------------------------------------------------------------------
# GET /api/v1/observability/breakdown/by-day
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_breakdown_by_day(client, chatbot_id, tenant_id):
    async with async_session_factory() as session:
        await _seed(session, tenant_id=tenant_id, chatbot_id=chatbot_id, tokens=200)

    resp = await client.get(
        "/api/v1/observability/breakdown/by-day",
        params={"days": 1},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    rows = resp.json()
    assert isinstance(rows, list)
    assert len(rows) >= 1
    row = rows[0]
    assert "day" in row
    assert "total_cost_usd" in row
    assert "total_tokens" in row
    assert "total_requests" in row


# ---------------------------------------------------------------------------
# GET /api/v1/observability/logs
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_logs_returns_items_with_all_fields(client, chatbot_id, tenant_id):
    ctx = RequestContext.new()
    async with async_session_factory() as session:
        await _seed(session, tenant_id=tenant_id, chatbot_id=chatbot_id,
                    tokens=100, latency_ms=150, ctx=ctx)

    resp = await client.get("/api/v1/observability/logs", headers=HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data
    assert "total" in data
    assert "has_more" in data
    assert data["total"] >= 1

    item = next((i for i in data["items"] if i["request_id"] == ctx.request_id), None)
    assert item is not None
    for field in ("id", "provider", "model", "phase", "direction", "tokens",
                  "cost_usd", "latency_ms", "request_id", "created_at"):
        assert field in item


@pytest.mark.asyncio
async def test_logs_phase_filter(client, chatbot_id, tenant_id):
    async with async_session_factory() as session:
        await _seed(session, tenant_id=tenant_id, chatbot_id=chatbot_id, phase="chat_response")
        await _seed(session, tenant_id=tenant_id, chatbot_id=None,
                    provider="openai", model="text-embedding-3-small",
                    phase="ingest_embed", direction="input", tokens=30)

    resp = await client.get(
        "/api/v1/observability/logs",
        params={"phase": "ingest_embed"},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert all(i["phase"] == "ingest_embed" for i in items)


@pytest.mark.asyncio
async def test_logs_provider_filter(client, chatbot_id, tenant_id):
    async with async_session_factory() as session:
        await _seed(session, tenant_id=tenant_id, chatbot_id=chatbot_id,
                    provider="anthropic", model="claude-sonnet-4-6", phase="chat_response")
        await _seed(session, tenant_id=tenant_id, chatbot_id=None,
                    provider="openai", model="text-embedding-3-small",
                    phase="embed_query", direction="input", tokens=20)

    resp = await client.get(
        "/api/v1/observability/logs",
        params={"provider": "openai"},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert all(i["provider"] == "openai" for i in items)


@pytest.mark.asyncio
async def test_logs_pagination(client, chatbot_id, tenant_id):
    async with async_session_factory() as session:
        for _ in range(3):
            await _seed(session, tenant_id=tenant_id, chatbot_id=chatbot_id, tokens=10)

    resp = await client.get(
        "/api/v1/observability/logs",
        params={"limit": 2, "offset": 0},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["items"]) <= 2
    if data["total"] > 2:
        assert data["has_more"] is True


# ---------------------------------------------------------------------------
# Auth & tenant isolation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_summary_requires_auth(client):
    resp = await client.get("/api/v1/observability/summary")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_tenant_isolation_summary(client, chatbot_id, tenant_id):
    """OWNER's logs are not visible to OTHER tenant."""
    async with async_session_factory() as session:
        await _seed(session, tenant_id=tenant_id, chatbot_id=chatbot_id, tokens=9999)

    resp = await client.get("/api/v1/observability/summary", headers=OTHER_HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    # OTHER has no logs; total_tokens must not include OWNER's 9999-token row
    assert data["total_tokens"] < 9999


@pytest.mark.asyncio
async def test_tenant_isolation_logs(client, chatbot_id, tenant_id):
    """OTHER tenant's logs endpoint returns only their own rows."""
    ctx = RequestContext.new()
    async with async_session_factory() as session:
        await _seed(session, tenant_id=tenant_id, chatbot_id=chatbot_id, ctx=ctx)

    resp = await client.get("/api/v1/observability/logs", headers=OTHER_HEADERS)
    assert resp.status_code == 200
    items = resp.json()["items"]
    request_ids = [i["request_id"] for i in items]
    assert ctx.request_id not in request_ids
