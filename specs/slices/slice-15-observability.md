# Slice 15 — Observability & Token Tracking

**Depends on:** Slice 8 (chat endpoint), Slice 11 (analytics dashboard)
**Prereq:** Read `00-prompt-prefix.md` first.

---

## Goal

Every LLM and embedding call gets logged with tokens, cost, and latency. A new "Observability" tab in the Gradio UI shows spend per chatbot, per day, and per request. No third-party metrics service — everything lives in Postgres and the Gradio dashboard.

## Deliverables

- `api/src/db/models.py` — `ObservationLog` model + `cost_rates` table
- `api/src/lib/observability.py` — `log_request()` helper + `CostCalculator`
- `api/src/config/observability.py` — token price constants
- `api/src/routes/observability.py` — observability API endpoints
- `api/alembic/versions/` — migration for new tables
- `api/src/lib/llm.py` — hook into `stream_completion()` to capture usage
- `api/src/lib/embedder.py` — capture usage on embedding calls
- `api/src/rag/rewrite.py` — capture usage on query rewrite calls
- `web/app.py` — Observability tab in Gradio
- `web/api_client.py` — observability API client methods

---

## Database Schema

### `observation_logs`

One row per LLM/embedding API call. Captures tokens, cost, latency, and context.

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | — |
| `tenant_id` | UUID FK → tenants | Denormalized |
| `chatbot_id` | UUID FK → chatbots | Nullable — embedding/rewrite calls |
| `provider` | TEXT | `anthropic` | `openai` |
| `model` | TEXT | `claude-sonnet-4-6`, `claude-haiku-4-5`, `text-embedding-3-small` |
| `phase` | TEXT | `chat_response` · `chat_rewrite` · `embed_query` · `embed_chunks` · `ingest_embed` |
| `direction` | TEXT | `input` · `output` |
| `tokens` | INT | Token count for this direction |
| `cost_usd` | NUMERIC(16,8) | Calculated cost in USD |
| `latency_ms` | INT | Milliseconds for this API call |
| `request_id` | TEXT | Opaque request ID (for tracing across phases) |
| `chunk_count` | INT NULL | Number of chunks embedded (for bulk calls) |
| `error` | TEXT NULL | Error message if the call failed |
| `created_at` | TIMESTAMP | Call timestamp |
| `deleted_at` | TIMESTAMP NULL | Soft delete |

Indexes:
- `(tenant_id, chatbot_id, created_at)` — for dashboard queries filtered by chatbot
- `(tenant_id, phase, created_at)` — for cost breakdown by phase
- `(tenant_id, created_at)` — for daily spend trends

### `cost_rates`

Price table. Maps model + direction → price per 1M tokens.

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | — |
| `provider` | TEXT | `anthropic` · `openai` |
| `model` | TEXT | Exact model string |
| `direction` | TEXT | `input` · `output` |
| `price_per_1m_tokens` | NUMERIC(16,6) | e.g. `3.00` = $3 per 1M input tokens |
| `updated_at` | TIMESTAMP | When this rate was last updated |
| `deleted_at` | TIMESTAMP NULL | Soft delete |

The table is seeded at startup with the latest public prices. It can be updated manually as prices change.

---

## Config — Token Prices (`api/src/config/observability.py`)

```python
from decimal import Decimal

# Prices per 1M tokens. Updated 2026-05-10 from vendor docs.

COST_RATES: dict[str, dict[str, dict[str, Decimal]]] = {
    "anthropic": {
        "claude-sonnet-4-6": {
            "input":  Decimal("3.00"),   # $3/1M input tokens
            "output": Decimal("15.00"),  # $15/1M output tokens
        },
        "claude-haiku-4-5": {
            "input":  Decimal("0.80"),   # $0.80/1M input tokens
            "output": Decimal("4.00"),   # $4/1M output tokens
        },
    },
    "openai": {
        "text-embedding-3-small": {
            "input":  Decimal("0.02"),   # $0.02/1M tokens (embeddings are input-only)
            "output": Decimal("0.00"),   # embeddings have no output tokens
        },
    },
}

def get_cost(provider: str, model: str, direction: str, tokens: int) -> Decimal:
    """Return cost in USD for a given provider/model/direction/tokens."""
    rate = COST_RATES[provider][model][direction]
    return (Decimal(tokens) / Decimal(1_000_000)) * rate
```

---

## Observability Helper (`api/src/lib/observability.py`)

```python
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from src.db.models import ObservationLog, CostRate
from src.config.observability import COST_RATES, get_cost
from src.lib.log import log

@dataclass
class RequestContext:
    """
    Carries a request_id through the chat flow.
    Each phase (rewrite, embed, chat_response) logs under the same request_id.
    """
    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    @classmethod
    def new(cls) -> "RequestContext":
        return cls()

async def log_request(
    db: AsyncSession,
    *,
    tenant_id: str,
    chatbot_id: str | None,
    provider: str,
    model: str,
    phase: str,
    direction: str,
    tokens: int,
    latency_ms: int,
    chunk_count: int = 0,
    error: str | None = None,
    request_context: RequestContext | None = None,
) -> str:
    """
    Log a single LLM/embedding API call.
    Returns the request_id for correlation.
    """
    cost = get_cost(provider, model, direction, tokens)

    row = ObservationLog(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        chatbot_id=chatbot_id,
        provider=provider,
        model=model,
        phase=phase,
        direction=direction,
        tokens=tokens,
        cost_usd=cost,
        latency_ms=latency_ms,
        request_id=(request_context.request_id if request_context else str(uuid.uuid4())),
        chunk_count=chunk_count,
        error=error,
        created_at=datetime.now(timezone.utc),
    )
    db.add(row)
    return row.request_id


@asynccontextmanager
async def measure_latency():
    """
    Context manager that yields a callable returning elapsed ms.
    Usage:
        async with measure_latency() as latency:
            result = await some_api_call()
            ms = latency()
    """
    start = time.perf_counter()
    yield lambda: int((time.perf_counter() - start) * 1000)


async def seed_cost_rates(db: AsyncSession) -> None:
    """
    Seed cost_rates table on startup. No-op if already seeded.
    """
    existing = (await db.execute(
        select(func.count(CostRate.id))
    )).scalar_one()
    if existing > 0:
        return

    now = datetime.now(timezone.utc)
    for provider, models in COST_RATES.items():
        for model, directions in models.items():
            for direction, price in directions.items():
                db.add(CostRate(
                    id=uuid.uuid4(),
                    provider=provider,
                    model=model,
                    direction=direction,
                    price_per_1m_tokens=price,
                    updated_at=now,
                ))
    await db.commit()
```

---

## Hook into LLM streaming (`api/src/lib/llm.py`)

Modify `stream_completion()` to yield the usage data along with the request_id so the chat route can log it.

```python
# Change UsageEvent to include request_id:

@dataclass
class UsageEvent:
    type: str = field(default='usage')
    input_tokens: int = field(default=0)
    output_tokens: int = field(default=0)
    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))  # added — for observability correlation
```

The chat route (Slice 8) will receive this `request_id` on the final `UsageEvent` and pass it to `log_request()`.

---

## Hook into embedder (`api/src/lib/embedder.py`)

Modify `embed_chunks()` to accept an optional `RequestContext`, and add a `count_tokens()` helper used by callers for logging:

```python
# In api/src/lib/embedder.py:

_encoding = get_encoding("cl100k_base")

def count_tokens(contents: list[str]) -> int:
    """Count tokens in a list of strings using tiktoken."""
    return sum(len(_encoding.encode(c)) for c in contents)

async def embed_chunks(
    contents: list[str],
    request_context=None,
) -> list[EmbeddingVector]:
    """Batches up to 100 per OpenAI call."""
```

**Note:** `embed_chunks` accepts `request_context` in its signature for API consistency but does **not** log internally. Logging for `embed_query` is done in `retrieve.py` and for `ingest_embed` in `worker/jobs.py`.

---

## Hook into query rewriting (`api/src/rag/rewrite.py`)

Add `request_context` parameter and a separate token-counting helper:

```python
async def rewrite_query(
    message: str,
    history: list[dict],
    request_context=None,
) -> str:
    """Rewrite follow-up into self-contained query. Returns original if history is empty or rewrite fails."""


def count_rewrite_tokens(message: str, history: list[dict]) -> int:
    """Count input tokens for the rewrite call (used by the chat route before calling rewrite_query)."""
    recent = history[-6:]
    transcript_lines = [f"{m['role'].upper()}: {m['content']}" for m in recent]
    prompt = (
        f"Conversation so far:\n" + "\n".join(transcript_lines) +
        f"\n\nNew message: {message}\n\nRewritten query:"
    )
    return len(_encoding.encode(prompt))
```

**Note:** `rewrite_query` accepts `request_context` in its signature but does **not** log internally. The chat route calls `count_rewrite_tokens()` before calling `rewrite_query()`, then logs with `log_request()` itself.

---

## Observability Routes (`api/src/routes/observability.py`)

```python
from datetime import date, timedelta, datetime, timezone
from decimal import Decimal
from uuid import UUID
from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, func, cast, Float, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.base import get_db
from src.db.models import ObservationLog, Chatbot, Tenant
from src.db.tenant_scope import tenant_where
from src.auth.authenticate import get_current_tenant, AuthenticatedTenant

router = APIRouter(prefix="/observability", tags=["observability"])


@router.get("/summary")
async def observability_summary(
    chatbot_id: UUID | None = Query(default=None),
    days: int = Query(default=30, ge=1, le=365),
    auth: AuthenticatedTenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    """
    Cost and token summary for the given window.
    Scoped to the authenticated tenant's chatbots.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    conditions = [
        ObservationLog.tenant_id == auth.tenant_id,
        ObservationLog.created_at >= cutoff,
    ]
    if chatbot_id:
        # Validate this chatbot belongs to the tenant before filtering
        owned = (await db.execute(
            select(Chatbot.id).where(tenant_where(Chatbot, auth.tenant_id), Chatbot.id == chatbot_id)
        )).scalar_one_or_none()
        if not owned:
            return {
                "total_cost_usd": 0.0,
                "total_tokens": 0,
                "total_requests": 0,
                "avg_cost_per_request": 0.0,
                "avg_latency_ms": 0,
            }
        conditions.append(ObservationLog.chatbot_id == chatbot_id)

    agg = (await db.execute(
        select(
            func.sum(ObservationLog.cost_usd).label("total_cost"),
            func.sum(ObservationLog.tokens).label("total_tokens"),
            func.count(ObservationLog.id).label("total_requests"),
            func.avg(ObservationLog.latency_ms).label("avg_latency"),
        )
        .where(*conditions)
    )).mappings().one()

    total_cost = float(agg["total_cost"] or 0)
    total_tokens = int(agg["total_tokens"] or 0)
    total_requests = int(agg["total_requests"] or 0)

    return {
        "total_cost_usd": round(total_cost, 4),
        "total_tokens": total_tokens,
        "total_requests": total_requests,
        "avg_cost_per_request": round(total_cost / total_requests, 4) if total_requests else 0.0,
        "avg_latency_ms": round(float(agg["avg_latency"] or 0), 0),
    }


@router.get("/breakdown/by-chatbot")
async def cost_by_chatbot(
    days: int = Query(default=30, ge=1, le=365),
    auth: AuthenticatedTenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    """
    Cost and token breakdown grouped by chatbot within the tenant.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    rows = (await db.execute(
        select(
            ObservationLog.chatbot_id,
            func.sum(ObservationLog.cost_usd).label("total_cost"),
            func.sum(ObservationLog.tokens).label("total_tokens"),
            func.count(ObservationLog.id).label("total_requests"),
        )
        .where(
            ObservationLog.tenant_id == auth.tenant_id,
            ObservationLog.chatbot_id.isnot(None),
            ObservationLog.created_at >= cutoff,
        )
        .group_by(ObservationLog.chatbot_id)
        .order_by(func.sum(ObservationLog.cost_usd).desc())
    )).mappings().all()

    # Fetch chatbot names
    chatbot_ids = [str(r["chatbot_id"]) for r in rows]
    chatbots = {
        str(c.id): c.name
        for c in (await db.execute(
            select(Chatbot).where(Chatbot.id.in_(chatbot_ids))
        )).scalars().all()
    }

    return [
        {
            "chatbot_id": r["chatbot_id"],
            "chatbot_name": chatbots.get(str(r["chatbot_id"]), "Unknown"),
            "total_cost_usd": round(float(r["total_cost"] or 0), 4),
            "total_tokens": int(r["total_tokens"] or 0),
            "total_requests": int(r["total_requests"] or 0),
        }
        for r in rows
    ]


@router.get("/breakdown/by-phase")
async def cost_by_phase(
    days: int = Query(default=30, ge=1, le=365),
    chatbot_id: UUID | None = Query(default=None),
    auth: AuthenticatedTenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    """
    Cost and token breakdown grouped by phase (chat_response, embed_chunks, etc.).
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    conditions = [
        ObservationLog.tenant_id == auth.tenant_id,
        ObservationLog.created_at >= cutoff,
    ]
    if chatbot_id:
        conditions.append(ObservationLog.chatbot_id == chatbot_id)

    rows = (await db.execute(
        select(
            ObservationLog.phase,
            func.sum(ObservationLog.cost_usd).label("total_cost"),
            func.sum(ObservationLog.tokens).label("total_tokens"),
            func.count(ObservationLog.id).label("total_requests"),
            func.avg(ObservationLog.latency_ms).label("avg_latency_ms"),
        )
        .where(*conditions)
        .group_by(ObservationLog.phase)
        .order_by(func.sum(ObservationLog.cost_usd).desc())
    )).mappings().all()

    return [
        {
            "phase": r["phase"],
            "total_cost_usd": round(float(r["total_cost"] or 0), 4),
            "total_tokens": int(r["total_tokens"] or 0),
            "total_requests": int(r["total_requests"] or 0),
            "avg_latency_ms": round(float(r["avg_latency_ms"] or 0), 0),
        }
        for r in rows
    ]


@router.get("/breakdown/by-day")
async def cost_by_day(
    days: int = Query(default=30, ge=1, le=365),
    chatbot_id: UUID | None = Query(default=None),
    auth: AuthenticatedTenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    """
    Daily cost and token trend. Timezone-aware (UTC).
    """
    bot_ids_q = select(Chatbot.id).where(tenant_where(Chatbot, auth.tenant_id))
    if chatbot_id:
        bot_ids_q = bot_ids_q.where(Chatbot.id == chatbot_id)
    bot_ids = [str(r) for (r,) in (await db.execute(bot_ids_q)).all()]

    condition = [
        ObservationLog.tenant_id == auth.tenant_id,
        ObservationLog.created_at >= datetime.now(timezone.utc) - timedelta(days=days),
    ]
    if bot_ids:
        condition.append(ObservationLog.chatbot_id.in_(bot_ids))

    rows = (await db.execute(
        select(
            func.date_trunc("day", ObservationLog.created_at).label("day"),
            func.sum(ObservationLog.cost_usd).label("total_cost"),
            func.sum(ObservationLog.tokens).label("total_tokens"),
            func.count(ObservationLog.id).label("total_requests"),
        )
        .where(*condition)
        .group_by(func.date_trunc("day", ObservationLog.created_at))
        .order_by("day")
    )).mappings().all()

    return [
        {
            "day": str(r["day"]),
            "total_cost_usd": round(float(r["total_cost"] or 0), 4),
            "total_tokens": int(r["total_tokens"] or 0),
            "total_requests": int(r["total_requests"] or 0),
        }
        for r in rows
    ]


@router.get("/logs")
async def list_logs(
    chatbot_id: UUID | None = Query(default=None),
    phase: str | None = Query(default=None),
    provider: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    auth: AuthenticatedTenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    """
    Recent observation logs. Paginated. Filterable by phase/provider/chatbot.
    """
    bot_ids_q = select(Chatbot.id).where(tenant_where(Chatbot, auth.tenant_id))
    if chatbot_id:
        bot_ids_q = bot_ids_q.where(Chatbot.id == chatbot_id)
    bot_ids = [str(r) for (r,) in (await db.execute(bot_ids_q)).all()]

    conditions = [
        ObservationLog.tenant_id == auth.tenant_id,
        ObservationLog.created_at >= datetime.now(timezone.utc) - timedelta(days=90),
    ]
    if bot_ids:
        conditions.append(ObservationLog.chatbot_id.in_(bot_ids))
    if phase:
        conditions.append(ObservationLog.phase == phase)
    if provider:
        conditions.append(ObservationLog.provider == provider)

    total = (await db.execute(
        select(func.count()).select_from(
            select(ObservationLog.id).where(*conditions).subquery()
        )
    )).scalar_one()

    rows = (await db.execute(
        select(ObservationLog)
        .where(*conditions)
        .order_by(ObservationLog.created_at.desc())
        .limit(limit)
        .offset(offset)
    )).scalars().all()

    return {
        "items": [
            {
                "id": str(r.id),
                "chatbot_id": str(r.chatbot_id) if r.chatbot_id else None,
                "provider": r.provider,
                "model": r.model,
                "phase": r.phase,
                "direction": r.direction,
                "tokens": r.tokens,
                "cost_usd": round(float(r.cost_usd), 6),
                "latency_ms": r.latency_ms,
                "chunk_count": r.chunk_count,
                "request_id": r.request_id,
                "error": r.error,
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ],
        "total": total,
        "has_more": total > offset + len(rows),
    }
```

Register in `main.py`:
```python
from src.routes.observability import router as observability_router
api_v1.include_router(observability_router)
```

---

## API client additions (`web/api_client.py`)

```python
async def get_observability_summary(
    self, chatbot_id: str | None = None, days: int = 30
) -> dict:
    params = {"days": days}
    if chatbot_id:
        params["chatbot_id"] = chatbot_id
    async with self._client() as c:
        r = await c.get("/api/v1/observability/summary", params=params)
        r.raise_for_status()
        return r.json()

async def get_cost_by_chatbot(self, days: int = 30) -> list[dict]:
    async with self._client() as c:
        r = await c.get("/api/v1/observability/breakdown/by-chatbot", params={"days": days})
        r.raise_for_status()
        return r.json()

async def get_cost_by_phase(self, chatbot_id: str | None = None, days: int = 30) -> list[dict]:
    params = {"days": days}
    if chatbot_id:
        params["chatbot_id"] = chatbot_id
    async with self._client() as c:
        r = await c.get("/api/v1/observability/breakdown/by-phase", params=params)
        r.raise_for_status()
        return r.json()

async def get_cost_by_day(self, chatbot_id: str | None = None, days: int = 30) -> list[dict]:
    params = {"days": days}
    if chatbot_id:
        params["chatbot_id"] = chatbot_id
    async with self._client() as c:
        r = await c.get("/api/v1/observability/breakdown/by-day", params=params)
        r.raise_for_status()
        return r.json()

async def get_observability_logs(
    self,
    chatbot_id: str | None = None,
    phase: str | None = None,
    provider: str | None = None,
    limit: int = 50,
) -> dict:
    params = {"limit": limit}
    if chatbot_id:
        params["chatbot_id"] = chatbot_id
    if phase:
        params["phase"] = phase
    if provider:
        params["provider"] = provider
    async with self._client() as c:
        r = await c.get("/api/v1/observability/logs", params=params)
        r.raise_for_status()
        return r.json()
```

---

## Gradio — Observability tab (`web/app.py`)

Add a fifth tab to the Gradio UI. It shows cost at a glance and lets you drill into request-level details.

```python
with gr.Tab("Observability"):
    gr.Markdown("## Token & Cost Tracking")
    gr.Markdown("Track LLM spend per chatbot, per phase, and per day. Prices reflect vendor public rates.")

    with gr.Row():
        obs_days = gr.Number(label="Lookback (days)", value=30, minimum=1, maximum=365, step=1)
        obs_chatbot_select = gr.Dropdown(label="Filter by chatbot", choices=["All"])
        refresh_obs_btn = gr.Button("Refresh", variant="secondary")

    # ── Summary cards ──────────────────────────────────────────
    with gr.Row():
        obs_total_cost = gr.Number(label="Total cost (USD)", interactive=False)
        obs_total_tokens = gr.Number(label="Total tokens", interactive=False)
        obs_total_requests = gr.Number(label="Total requests", interactive=False)
        obs_avg_cost = gr.Number(label="Avg cost/request (USD)", interactive=False)

    # ── Cost by chatbot ────────────────────────────────────────
    gr.Markdown("### Cost by chatbot")
    chatbot_cost_table = gr.Dataframe(
        headers=["Chatbot", "Cost (USD)", "Tokens", "Requests"],
        interactive=False,
    )

    # ── Cost by phase ──────────────────────────────────────────
    gr.Markdown("### Cost by phase")
    phase_cost_table = gr.Dataframe(
        headers=["Phase", "Cost (USD)", "Tokens", "Requests", "Avg Latency (ms)"],
        interactive=False,
    )

    # ── Daily spend ────────────────────────────────────────────
    gr.Markdown("### Daily spend")
    daily_cost_table = gr.Dataframe(
        headers=["Day", "Cost (USD)", "Tokens", "Requests"],
        interactive=False,
    )

    # ── Recent logs ────────────────────────────────────────────
    gr.Markdown("### Recent requests")
    log_filter_phase = gr.Dropdown(
        label="Filter by phase",
        choices=["All", "chat_response", "chat_rewrite", "embed_query", "embed_chunks", "ingest_embed"],
        value="All",
    )
    log_filter_provider = gr.Dropdown(
        label="Filter by provider",
        choices=["All", "anthropic", "openai"],
        value="All",
    )
    logs_table = gr.Dataframe(
        headers=["Time", "Provider", "Model", "Phase", "Direction", "Tokens", "Cost", "Latency (ms)"],
        interactive=False,
    )


def refresh_observability(token, days, chatbot_name, chatbot_choices, phase_filter, provider_filter):
    client = get_client(token)
    chatbot_id = chatbot_choices.get(chatbot_name) if chatbot_name != "All" else None
    phase = phase_filter if phase_filter != "All" else None
    provider = provider_filter if provider_filter != "All" else None

    summary = run(client.get_observability_summary(chatbot_id, days))
    by_chatbot = run(client.get_cost_by_chatbot(days))
    by_phase = run(client.get_cost_by_phase(chatbot_id, days))
    by_day = run(client.get_cost_by_day(chatbot_id, days))
    logs = run(client.get_observability_logs(chatbot_id, phase=phase, provider=provider, limit=100))

    # Cost by chatbot
    cb_rows = [
        [r["chatbot_name"], round(r["total_cost_usd"], 4), r["total_tokens"], r["total_requests"]]
        for r in by_chatbot
    ]

    # Cost by phase
    ph_rows = [
        [
            r["phase"],
            round(r["total_cost_usd"], 4),
            r["total_tokens"],
            r["total_requests"],
            int(r["avg_latency_ms"]),
        ]
        for r in by_phase
    ]

    # Daily spend
    dd_rows = [
        [r["day"], round(r["total_cost_usd"], 4), r["total_tokens"], r["total_requests"]]
        for r in by_day
    ]

    # Recent logs
    lg_rows = [
        [
            entry["created_at"][:19],
            entry["provider"],
            entry["model"],
            entry["phase"],
            entry["direction"],
            entry["tokens"],
            round(entry["cost_usd"], 6),
            entry["latency_ms"],
        ]
        for entry in logs.get("items", [])
    ]

    return (
        summary.get("total_cost_usd", 0),
        summary.get("total_tokens", 0),
        summary.get("total_requests", 0),
        summary.get("avg_cost_per_request", 0),
        gr.Dataframe(value=cb_rows),
        gr.Dataframe(value=ph_rows),
        gr.Dataframe(value=dd_rows),
        gr.Dataframe(value=lg_rows),
    )


_obs_inputs = [token_input, obs_days, obs_chatbot_select, chatbot_choices_state, log_filter_phase, log_filter_provider]
_obs_outputs = [obs_total_cost, obs_total_tokens, obs_total_requests, obs_avg_cost,
                chatbot_cost_table, phase_cost_table, daily_cost_table, logs_table]

refresh_obs_btn.click(refresh_observability, inputs=_obs_inputs, outputs=_obs_outputs)
obs_days.change(refresh_observability, inputs=_obs_inputs, outputs=_obs_outputs)
obs_chatbot_select.change(refresh_observability, inputs=_obs_inputs, outputs=_obs_outputs)
log_filter_phase.change(refresh_observability, inputs=_obs_inputs, outputs=_obs_outputs)
log_filter_provider.change(refresh_observability, inputs=_obs_inputs, outputs=_obs_outputs)
```

---

## Integration Points — What changes in existing slices

### Chat route (Slice 8) — `api/src/routes/chat.py`

In `_generate()`, wrap the chat flow with a `RequestContext` and log the final usage:

```python
from src.lib.observability import RequestContext, log_request
from src.db.base import async_session

# ... inside _generate(), after the streaming loop:

        # Persist assistant message
        no_answer = "I don't have information about that" in assistant_text

        # Log the chat_response call (both directions in one transaction)
        async with async_session() as log_db:
            await log_request(
                log_db,
                tenant_id=str(chatbot.tenant_id),
                chatbot_id=str(chatbot.id),
                provider="anthropic",
                model="claude-sonnet-4-6",
                phase="chat_response",
                direction="input",
                tokens=input_tokens,
                latency_ms=chat_latency,  # captured via measure_latency() around the streaming loop
                request_context=request_ctx,
            )
            await log_request(
                log_db,
                tenant_id=str(chatbot.tenant_id),
                chatbot_id=str(chatbot.id),
                provider="anthropic",
                model="claude-sonnet-4-6",
                phase="chat_response",
                direction="output",
                tokens=output_tokens,
                latency_ms=chat_latency,
                request_context=request_ctx,
            )
            await log_db.commit()  # single commit for both rows
```

Also log the query rewrite call. Tokens are counted with `count_rewrite_tokens()` **before** calling `rewrite_query()`. Logging only happens when the query was actually rewritten (i.e. `retrieval_query != body.message`):

```python
        # Query rewriting
        rewrite_ctx = RequestContext.new()
        rewrite_tokens = count_rewrite_tokens(body.message, prior_history) if prior_history else 0
        async with measure_latency() as lat:
            retrieval_query = await rewrite_query(
                body.message, prior_history,
                request_context=rewrite_ctx,
            )
        rewrite_latency = lat()

        if retrieval_query != body.message:
            async with async_session() as log_db:
                await log_request(
                    log_db,
                    tenant_id=str(chatbot.tenant_id),
                    chatbot_id=str(chatbot.id),
                    provider="anthropic",
                    model="claude-haiku-4-5-20251001",
                    phase="chat_rewrite",
                    direction="input",
                    tokens=rewrite_tokens,
                    latency_ms=rewrite_latency,
                    request_context=rewrite_ctx,
                )
                await log_db.commit()
```

### Retrieval (Slice 7) — `api/src/rag/retrieve.py`

The `retrieve_context()` function calls `embed_chunks([query])` for the query. Pass an observation context so it logs:

```python
async def retrieve_context(
    *,
    chatbot_id: str,
    query: str,
    top_k: int = 5,
    min_similarity: float = 0.75,
    request_context: RequestContext | None = None,
) -> list[RetrievedChunk]:
    """
    If request_context is provided, logs the embed_query call.
    """
```

### Worker (Slice 6) — `api/src/worker/jobs.py`

During ingestion, the worker embeds chunks in batches of 100. Total latency is measured with `time.perf_counter()` around the entire batching loop, then logged once as `ingest_embed`:

```python
# In ingest_document():
import time
from src.lib.observability import log_request
from src.db.base import async_session

# ... after parsing and chunking:
total_tokens = sum(c.token_count for c in draft_chunks)
embed_start = time.perf_counter()
for i in range(0, len(draft_chunks), BATCH):
    batch = draft_chunks[i:i + BATCH]
    embeddings = await embed_chunks([c.content for c in batch])
    # ... insert batch into DB ...
embed_latency = int((time.perf_counter() - embed_start) * 1000)

async with async_session() as log_db:
    await log_request(
        log_db,
        tenant_id=str(doc.tenant_id),
        chatbot_id=str(doc.chatbot_id),
        provider="openai",
        model="text-embedding-3-small",
        phase="ingest_embed",
        direction="input",
        tokens=total_tokens,
        latency_ms=embed_latency,
        chunk_count=len(draft_chunks),
    )
    await log_db.commit()
```

---

## Alembic Migration

Generate with: `docker compose exec api alembic revision -m "observability_tables"`

```python
"""observability_tables

Revision ID: obs_001
Revises: <last_existing_migration>
Create Date: 2026-05-10
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'obs_001'
down_revision = '<last_existing_migration>'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Cost rates table
    op.create_table(
        'cost_rates',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('provider', sa.Text(), nullable=False),
        sa.Column('model', sa.Text(), nullable=False),
        sa.Column('direction', sa.Text(), nullable=False),
        sa.Column('price_per_1m_tokens', postgresql.NUMERIC(precision=16, scale=6), nullable=False),
        sa.Column('updated_at', sa.TIMESTAMP(), nullable=False),
        sa.Column('deleted_at', sa.TIMESTAMP(), nullable=True),
    )
    op.create_unique_constraint(
        'uq_cost_rates',
        'cost_rates', ['provider', 'model', 'direction', 'deleted_at']
    )

    # Observation logs table
    op.create_table(
        'observation_logs',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('tenant_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('tenants.id', ondelete='CASCADE'), nullable=False),
        sa.Column('chatbot_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('chatbots.id', ondelete='SET NULL'), nullable=True),
        sa.Column('provider', sa.Text(), nullable=False),
        sa.Column('model', sa.Text(), nullable=False),
        sa.Column('phase', sa.Text(), nullable=False),
        sa.Column('direction', sa.Text(), nullable=False),
        sa.Column('tokens', sa.Integer(), nullable=False),
        sa.Column('cost_usd', postgresql.NUMERIC(precision=16, scale=8), nullable=False),
        sa.Column('latency_ms', sa.Integer(), nullable=False),
        sa.Column('request_id', sa.Text(), nullable=False),
        sa.Column('chunk_count', sa.Integer(), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.TIMESTAMP(), nullable=False),
        sa.Column('deleted_at', sa.TIMESTAMP(), nullable=True),
    )

    # Indexes for dashboard queries
    op.create_index(
        'ix_obs_logs_tenant_chatbot_created',
        'observation_logs', ['tenant_id', 'chatbot_id', 'created_at']
    )
    op.create_index(
        'ix_obs_logs_tenant_phase_created',
        'observation_logs', ['tenant_id', 'phase', 'created_at']
    )
    op.create_index(
        'ix_obs_logs_tenant_created',
        'observation_logs', ['tenant_id', 'created_at']
    )


def downgrade() -> None:
    op.drop_index('ix_obs_logs_tenant_created')
    op.drop_index('ix_obs_logs_tenant_phase_created')
    op.drop_index('ix_obs_logs_tenant_chatbot_created')
    op.drop_table('observation_logs')
    op.drop_table('cost_rates')
```

---

## Startup — seed cost rates

In `api/src/main.py`, add cost rate seeding to the lifespan:

```python
from src.lib.observability import seed_cost_rates
from src.db.base import async_session

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Seed cost rates on startup
    async with async_session() as db:
        await seed_cost_rates(db)
    yield
```

---

## Acceptance criteria

- `GET /api/v1/observability/summary` returns correct totals after a chat request is made.
- `GET /api/v1/observability/breakdown/by-chatbot` returns one row per chatbot with correct cost.
- `GET /api/v1/observability/breakdown/by-phase` returns rows for all phases that have logs.
- `GET /api/v1/observability/breakdown/by-day` returns daily rows covering the requested window.
- `GET /api/v1/observability/logs` returns paginated request logs with all fields.
- The Observability tab in Gradio shows summary cards, cost-by-chatbot, cost-by-phase, daily spend, and recent logs.
- Token costs are calculated correctly: `$3.00 / 1M * input_tokens` for Claude Sonnet input.
- A tenant cannot see observability data for another tenant's chatbots (404 on filtered queries).
- `cost_rates` table is seeded with default prices on first startup.
- Observation logs are persisted for all chat_response, chat_rewrite, embed_query, embed_chunks, and ingest_embed phases.
