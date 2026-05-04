from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, func, cast, Float, literal_column
from sqlalchemy.ext.asyncio import AsyncSession
from uuid import UUID

from src.db.base import get_db
from src.db.models import Chatbot, Conversation, Message
from src.db.tenant_scope import tenant_where
from src.auth.authenticate import get_current_tenant, AuthenticatedTenant

router = APIRouter(prefix="/analytics", tags=["analytics"])


@router.get("/summary")
async def analytics_summary(
    chatbot_id: UUID | None = Query(default=None),
    auth: AuthenticatedTenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    bot_ids_q = select(Chatbot.id).where(tenant_where(Chatbot, auth.tenant_id))
    if chatbot_id:
        bot_ids_q = bot_ids_q.where(Chatbot.id == chatbot_id)
    bot_ids = [r for (r,) in (await db.execute(bot_ids_q)).all()]

    if not bot_ids:
        return {
            "total_conversations": 0,
            "total_messages": 0,
            "no_answer_count": 0,
            "no_answer_rate": 0.0,
            "avg_top_similarity": None,
        }

    total_convs = (await db.execute(
        select(func.count(Conversation.id))
        .where(Conversation.chatbot_id.in_(bot_ids))
    )).scalar_one()

    msg_stats = (await db.execute(
        select(
            func.count(Message.id).label("total"),
            func.count(Message.id).filter(Message.no_answer == True).label("no_answer_count"),
        )
        .join(Conversation, Message.conversation_id == Conversation.id)
        .where(
            Conversation.chatbot_id.in_(bot_ids),
            Message.role == "assistant",
        )
    )).mappings().one()

    total_msgs = int(msg_stats["total"] or 0)
    no_answer_count = int(msg_stats["no_answer_count"] or 0)
    no_answer_rate = round(no_answer_count / total_msgs, 3) if total_msgs else 0.0

    avg_sim_result = (await db.execute(
        select(
            func.avg(
                cast(
                    func.jsonb_path_query_first(
                        Message.source_chunks,
                        literal_column("'$[0].similarity'::jsonpath"),
                    ),
                    Float,
                )
            )
        )
        .join(Conversation, Message.conversation_id == Conversation.id)
        .where(
            Conversation.chatbot_id.in_(bot_ids),
            Message.role == "assistant",
            Message.source_chunks.isnot(None),
            Message.no_answer == False,
        )
    )).scalar_one()

    return {
        "total_conversations": total_convs,
        "total_messages": total_msgs,
        "no_answer_count": no_answer_count,
        "no_answer_rate": no_answer_rate,
        "avg_top_similarity": round(float(avg_sim_result), 3) if avg_sim_result else None,
    }


@router.get("/conversations")
async def list_conversations(
    chatbot_id: UUID | None = Query(default=None),
    no_answer_only: bool = Query(default=False),
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    auth: AuthenticatedTenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    bot_ids_q = select(Chatbot.id).where(tenant_where(Chatbot, auth.tenant_id))
    if chatbot_id:
        bot_ids_q = bot_ids_q.where(Chatbot.id == chatbot_id)
    bot_ids = [r for (r,) in (await db.execute(bot_ids_q)).all()]

    if not bot_ids:
        return {"items": [], "total": 0, "has_more": False}

    has_failure = (
        select(func.bool_or(Message.no_answer))
        .where(
            Message.conversation_id == Conversation.id,
            Message.role == "assistant",
        )
        .scalar_subquery()
    )

    first_question = (
        select(Message.content)
        .where(
            Message.conversation_id == Conversation.id,
            Message.role == "user",
        )
        .order_by(Message.created_at)
        .limit(1)
        .scalar_subquery()
    )

    query = (
        select(
            Conversation.id,
            Conversation.chatbot_id,
            Conversation.created_at,
            first_question.label("first_question"),
            has_failure.label("has_failure"),
        )
        .where(Conversation.chatbot_id.in_(bot_ids))
    )

    if no_answer_only:
        query = query.where(has_failure)

    total = (await db.execute(
        select(func.count()).select_from(query.subquery())
    )).scalar_one()

    rows = (await db.execute(
        query.order_by(Conversation.created_at.desc()).limit(limit).offset(offset)
    )).mappings().all()

    return {
        "items": [dict(r) for r in rows],
        "total": total,
        "has_more": total > offset + len(rows),
    }


@router.get("/conversations/{conversation_id}/messages")
async def get_conversation_messages(
    conversation_id: UUID,
    auth: AuthenticatedTenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    bot_ids = [r for (r,) in (await db.execute(
        select(Chatbot.id).where(tenant_where(Chatbot, auth.tenant_id))
    )).all()]

    conv = (await db.execute(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.chatbot_id.in_(bot_ids),
        )
    )).scalar_one_or_none()

    if conv is None:
        from src.lib.errors import NotFoundError
        raise NotFoundError()

    messages = (await db.execute(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at)
    )).scalars().all()

    return {
        "conversation_id": str(conversation_id),
        "messages": [
            {
                "id": str(m.id),
                "role": m.role,
                "content": m.content,
                "no_answer": m.no_answer,
                "tokens_used": m.tokens_used,
                "source_chunks": m.source_chunks or [],
                "created_at": m.created_at.isoformat(),
            }
            for m in messages
        ],
    }
