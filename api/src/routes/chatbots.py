from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, func
from datetime import datetime, timezone
from uuid import UUID

from src.db.base import get_db
from src.db.models import Chatbot, Document
from src.db.tenant_scope import tenant_where
from src.auth.authenticate import get_current_tenant, AuthenticatedTenant
from src.schemas.chatbots import (
    CreateChatbotRequest,
    UpdateChatbotRequest,
    ChatbotResponse,
    ChatbotListResponse,
)
from src.lib.errors import NotFoundError

router = APIRouter(prefix="/chatbots", tags=["chatbots"])


@router.post("", status_code=201, response_model=dict)
async def create_chatbot(
    body: CreateChatbotRequest,
    auth: AuthenticatedTenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    chatbot = Chatbot(
        tenant_id=auth.tenant_id,
        name=body.name,
        system_prompt_override=body.system_prompt_override,
    )
    db.add(chatbot)
    await db.commit()
    await db.refresh(chatbot)
    return {"chatbot": ChatbotResponse.model_validate(chatbot).model_dump(mode="json")}


@router.get("", response_model=ChatbotListResponse)
async def list_chatbots(
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    auth: AuthenticatedTenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    base = select(Chatbot).where(tenant_where(Chatbot, auth.tenant_id))
    total = (
        await db.execute(select(func.count()).select_from(base.subquery()))
    ).scalar_one()
    rows = (
        await db.execute(
            base.order_by(Chatbot.created_at, Chatbot.id).limit(limit).offset(offset)
        )
    ).scalars().all()
    return ChatbotListResponse(
        items=[ChatbotResponse.model_validate(r) for r in rows],
        total=total,
        has_more=total > offset + len(rows),
    )


@router.get("/{chatbot_id}", response_model=dict)
async def get_chatbot(
    chatbot_id: UUID,
    auth: AuthenticatedTenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    row = (
        await db.execute(
            select(Chatbot).where(
                tenant_where(Chatbot, auth.tenant_id, Chatbot.id == chatbot_id)
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError()
    return {"chatbot": ChatbotResponse.model_validate(row).model_dump(mode="json")}


@router.put("/{chatbot_id}", response_model=dict)
async def update_chatbot(
    chatbot_id: UUID,
    body: UpdateChatbotRequest,
    auth: AuthenticatedTenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    row = (
        await db.execute(
            select(Chatbot).where(
                tenant_where(Chatbot, auth.tenant_id, Chatbot.id == chatbot_id)
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError()

    for key, value in body.model_dump(exclude_unset=True).items():
        setattr(row, key, value)

    await db.commit()
    await db.refresh(row)
    return {"chatbot": ChatbotResponse.model_validate(row).model_dump(mode="json")}


@router.delete("/{chatbot_id}", status_code=204)
async def delete_chatbot(
    chatbot_id: UUID,
    auth: AuthenticatedTenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    row = (
        await db.execute(
            select(Chatbot).where(
                tenant_where(Chatbot, auth.tenant_id, Chatbot.id == chatbot_id)
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError()

    now = datetime.now(timezone.utc)
    row.deleted_at = now

    await db.execute(
        update(Document)
        .where(Document.chatbot_id == chatbot_id, Document.deleted_at.is_(None))
        .values(deleted_at=now)
    )
    await db.commit()
