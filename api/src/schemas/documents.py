from pydantic import BaseModel
from uuid import UUID
from datetime import datetime
from typing import Literal


class DocumentResponse(BaseModel):
    id: UUID
    chatbot_id: UUID
    tenant_id: UUID
    filename: str
    mime_type: str
    status: Literal['pending', 'processing', 'ready', 'error']
    page_count: int | None
    error_reason: str | None
    created_at: datetime
    model_config = {"from_attributes": True}


class DocumentListResponse(BaseModel):
    items: list[DocumentResponse]
    total: int
    has_more: bool
