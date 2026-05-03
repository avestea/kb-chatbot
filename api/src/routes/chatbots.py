from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.authenticate import get_current_tenant, AuthenticatedTenant
from src.db.base import get_db
from src.db.models import Chatbot
from src.db.tenant_scope import tenant_where

router = APIRouter()


@router.get("/chatbots")
async def list_chatbots(
    auth: AuthenticatedTenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Chatbot).where(tenant_where(Chatbot, auth.tenant_id))
        .order_by(Chatbot.created_at, Chatbot.id)
    )
    chatbots = result.scalars().all()
    return {
        "items": [
            {
                "id": str(c.id),
                "tenant_id": str(c.tenant_id),
                "name": c.name,
                "system_prompt_override": c.system_prompt_override,
                "deleted_at": c.deleted_at.isoformat() if c.deleted_at else None,
                "created_at": c.created_at.isoformat(),
            }
            for c in chatbots
        ],
        "total": len(chatbots),
        "has_more": False,
    }
