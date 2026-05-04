from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy import select
from uuid import UUID

from src.db.base import get_db
from src.db.models import Feedback, Message, Conversation, Chatbot
from src.db.tenant_scope import tenant_where
from src.auth.authenticate import get_current_tenant, AuthenticatedTenant
from src.lib.errors import NotFoundError

router = APIRouter(prefix="/feedback", tags=["feedback"])


class SubmitFeedbackRequest(BaseModel):
    message_id: UUID
    rating: int = Field(..., ge=-1, le=1)

    @model_validator(mode="after")
    def rating_not_zero(self) -> "SubmitFeedbackRequest":
        if self.rating == 0:
            raise ValueError("rating must be -1 or 1")
        return self


@router.post("", status_code=201)
async def submit_feedback(
    body: SubmitFeedbackRequest,
    auth: AuthenticatedTenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    bot_ids = [r for (r,) in (await db.execute(
        select(Chatbot.id).where(tenant_where(Chatbot, auth.tenant_id))
    )).all()]

    message = (await db.execute(
        select(Message)
        .join(Conversation, Message.conversation_id == Conversation.id)
        .where(
            Message.id == body.message_id,
            Message.role == "assistant",
            Conversation.chatbot_id.in_(bot_ids),
        )
    )).scalar_one_or_none()

    if message is None:
        raise NotFoundError("Message not found")

    conversation = (await db.execute(
        select(Conversation).where(Conversation.id == message.conversation_id)
    )).scalar_one()

    stmt = pg_insert(Feedback).values(
        message_id=body.message_id,
        chatbot_id=conversation.chatbot_id,
        tenant_id=auth.tenant_id,
        rating=body.rating,
    ).on_conflict_do_update(
        index_elements=["message_id"],
        set_={"rating": body.rating},
    )
    await db.execute(stmt)
    await db.commit()

    return {"message_id": str(body.message_id), "rating": body.rating}
