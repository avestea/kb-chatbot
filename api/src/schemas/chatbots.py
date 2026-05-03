from pydantic import BaseModel, Field
from uuid import UUID
from datetime import datetime


class CreateChatbotRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    system_prompt_override: str | None = Field(None, max_length=4000)


class UpdateChatbotRequest(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=100)
    system_prompt_override: str | None = Field(None, max_length=4000)


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
