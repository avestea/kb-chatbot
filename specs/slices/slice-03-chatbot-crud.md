# Slice 3 — Chatbot CRUD

**Depends on:** Slice 1, Slice 2
**Prereq:** Read `00-prompt-prefix.md` first.

---

## Goal

Tenants can create and manage chatbots. No document upload yet.

## Deliverables

- `api/src/schemas/chatbots.py` — Pydantic request/response models.
- `api/src/routes/chatbots.py` — FastAPI router with full CRUD.
- Register router in `main.py` under `/api/v1/chatbots`.

## Schemas (`api/src/schemas/chatbots.py`)

```python
from pydantic import BaseModel, Field
from uuid import UUID
from datetime import datetime
from typing import Literal

class CreateChatbotRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    system_prompt_override: str | None = Field(None, max_length=4000)

class UpdateChatbotRequest(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=100)
    system_prompt_override: str | None = Field(None, max_length=4000)
    # Distinguish "omitted" from "explicitly set to null" with a sentinel
    # Use UNSET pattern: if field is absent, don't update it

class ChatbotResponse(BaseModel):
    id: UUID
    tenant_id: UUID
    name: str
    system_prompt_override: str | None
    created_at: datetime
    model_config = {"from_attributes": True}

class ChatbotListResponse(BaseModel):
    items: list[ChatbotResponse]
    total: int
    has_more: bool
```

## Router (`api/src/routes/chatbots.py`)

```python
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
    CreateChatbotRequest, UpdateChatbotRequest,
    ChatbotResponse, ChatbotListResponse,
)
from src.lib.errors import NotFoundError, ValidationError

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
    return {"chatbot": ChatbotResponse.model_validate(chatbot)}

@router.get("", response_model=ChatbotListResponse)
async def list_chatbots(
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    auth: AuthenticatedTenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    base = select(Chatbot).where(tenant_where(Chatbot, auth.tenant_id))
    total = (await db.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
    rows = (await db.execute(
        base.order_by(Chatbot.created_at, Chatbot.id).limit(limit).offset(offset)
    )).scalars().all()
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
    row = (await db.execute(
        select(Chatbot).where(tenant_where(Chatbot, auth.tenant_id, Chatbot.id == chatbot_id))
    )).scalar_one_or_none()
    if row is None:
        raise NotFoundError()
    return {"chatbot": ChatbotResponse.model_validate(row)}

@router.put("/{chatbot_id}", response_model=dict)
async def update_chatbot(
    chatbot_id: UUID,
    body: UpdateChatbotRequest,
    auth: AuthenticatedTenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    row = (await db.execute(
        select(Chatbot).where(tenant_where(Chatbot, auth.tenant_id, Chatbot.id == chatbot_id))
    )).scalar_one_or_none()
    if row is None:
        raise NotFoundError()

    updates = body.model_dump(exclude_unset=True)
    for key, value in updates.items():
        setattr(row, key, value)

    await db.commit()
    await db.refresh(row)
    return {"chatbot": ChatbotResponse.model_validate(row)}

@router.delete("/{chatbot_id}", status_code=204)
async def delete_chatbot(
    chatbot_id: UUID,
    auth: AuthenticatedTenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    row = (await db.execute(
        select(Chatbot).where(tenant_where(Chatbot, auth.tenant_id, Chatbot.id == chatbot_id))
    )).scalar_one_or_none()
    if row is None:
        raise NotFoundError()

    now = datetime.now(timezone.utc)
    row.deleted_at = now

    # Cascade soft-delete to documents
    await db.execute(
        update(Document)
        .where(Document.chatbot_id == chatbot_id, Document.deleted_at.is_(None))
        .values(deleted_at=now)
    )
    await db.commit()
```

## Register in `main.py`

```python
from src.routes.chatbots import router as chatbots_router

api_v1 = APIRouter(prefix="/api/v1")
api_v1.include_router(chatbots_router)
app.include_router(api_v1)
```

## Acceptance criteria

- Full CRUD cycle works via curl.
- `POST /api/v1/chatbots` → 201 `{"chatbot": {...}}`.
- `GET /api/v1/chatbots` → `{"items":[...],"total":N,"has_more":false}`.
- `GET /api/v1/chatbots?limit=200` → 422 validation error.
- Tenant A cannot GET/PUT/DELETE a chatbot owned by Tenant B (returns 404, not 403).
- DELETE soft-deletes the chatbot and all its documents; chatbot disappears from GET list.
- `PUT` with only `{"name": "New Name"}` updates only name; `system_prompt_override` unchanged.
