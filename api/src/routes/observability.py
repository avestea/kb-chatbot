"""Observability API endpoints — cost, tokens, latency per chatbot/phase/day."""

from datetime import timedelta, datetime, timezone
from uuid import UUID
from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, func, literal_column
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.base import get_db
from src.db.models import ObservationLog, Chatbot
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
    bot_ids = [r for (r,) in (await db.execute(bot_ids_q)).all()]

    condition = [
        ObservationLog.tenant_id == auth.tenant_id,
        ObservationLog.created_at >= datetime.now(timezone.utc) - timedelta(days=days),
    ]
    if bot_ids:
        condition.append(ObservationLog.chatbot_id.in_(bot_ids))

    day_expr = func.date_trunc(literal_column("'day'"), ObservationLog.created_at).label("day")
    rows = (await db.execute(
        select(
            day_expr,
            func.sum(ObservationLog.cost_usd).label("total_cost"),
            func.sum(ObservationLog.tokens).label("total_tokens"),
            func.count(ObservationLog.id).label("total_requests"),
        )
        .where(*condition)
        .group_by(day_expr)
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
