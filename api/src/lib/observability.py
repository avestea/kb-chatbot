"""Observability: log LLM/embedding calls with tokens, cost, latency."""

import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from src.config.observability import COST_RATES, get_cost
from src.db.models import ObservationLog, CostRate


@dataclass
class RequestContext:
    """Carries a request_id through the chat flow."""
    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    @classmethod
    def new(cls) -> "RequestContext":
        return cls()


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
        tenant_id=uuid.UUID(tenant_id),
        chatbot_id=uuid.UUID(chatbot_id) if chatbot_id else None,
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
