# Slice 4 — Document Upload & Storage

**Depends on:** Slice 1, Slice 2, Slice 3
**Prereq:** Read `00-prompt-prefix.md` first.

---

## Goal

Users can upload files. Files land in S3/MinIO. DB record created with `status: pending`. ARQ job enqueued. No parsing yet.

## Deliverables

- `api/src/lib/s3.py` — aioboto3 client singleton.
- `api/src/schemas/documents.py` — Pydantic response models.
- `api/src/routes/documents.py` — upload, list, delete endpoints.
- Register router in `main.py` under `/api/v1/chatbots/{chatbot_id}/documents`.

## Allowed MIME types

```python
ALLOWED_MIME_TYPES = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "text/plain",
    "text/html",
}
MAX_FILE_SIZE = 20 * 1024 * 1024  # 20 MB
```

## S3 client (`api/src/lib/s3.py`)

```python
import aioboto3
from src.config.env import settings

_session = aioboto3.Session(
    aws_access_key_id=settings.S3_ACCESS_KEY_ID,
    aws_secret_access_key=settings.S3_SECRET_ACCESS_KEY,
    region_name=settings.S3_REGION,
)

def s3_client():
    """Use as async context manager: async with s3_client() as s3: ..."""
    kwargs = {}
    if settings.S3_ENDPOINT:
        kwargs["endpoint_url"] = settings.S3_ENDPOINT
    return _session.client("s3", **kwargs)
```

**S3 key convention:** `{tenant_id}/{chatbot_id}/{document_id}/{filename}`

## Schemas (`api/src/schemas/documents.py`)

```python
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
```

## Routes (`api/src/routes/documents.py`)

```python
from fastapi import APIRouter, Depends, UploadFile, File, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, delete, func
from datetime import datetime, timezone
from uuid import UUID, uuid4

from src.db.base import get_db
from src.db.models import Chatbot, Document, Chunk
from src.db.tenant_scope import tenant_where
from src.auth.authenticate import get_current_tenant, AuthenticatedTenant
from src.lib.s3 import s3_client
from src.lib.errors import NotFoundError, UnsupportedMimeTypeError, PayloadTooLargeError
from src.config.env import settings
from src.schemas.documents import DocumentResponse, DocumentListResponse

ALLOWED_MIME_TYPES = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "text/plain",
    "text/html",
}
MAX_FILE_SIZE = 20 * 1024 * 1024

router = APIRouter(prefix="/chatbots/{chatbot_id}/documents", tags=["documents"])

@router.post("", status_code=201, response_model=dict)
async def upload_document(
    chatbot_id: UUID,
    file: UploadFile = File(...),
    auth: AuthenticatedTenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    # Verify chatbot belongs to tenant
    chatbot = (await db.execute(
        select(Chatbot).where(tenant_where(Chatbot, auth.tenant_id, Chatbot.id == chatbot_id))
    )).scalar_one_or_none()
    if chatbot is None:
        raise NotFoundError("Chatbot not found")

    # Validate MIME type
    mime_type = file.content_type or ""
    if mime_type not in ALLOWED_MIME_TYPES:
        raise UnsupportedMimeTypeError(mime_type)

    # Read file and check size
    data = await file.read()
    if len(data) > MAX_FILE_SIZE:
        raise PayloadTooLargeError("File exceeds 20MB limit")

    document_id = uuid4()
    s3_key = f"{auth.tenant_id}/{chatbot_id}/{document_id}/{file.filename}"

    # Upload to S3
    async with s3_client() as s3:
        await s3.put_object(
            Bucket=settings.S3_BUCKET,
            Key=s3_key,
            Body=data,
            ContentType=mime_type,
        )

    # Create document record
    doc = Document(
        id=document_id,
        chatbot_id=chatbot_id,
        tenant_id=auth.tenant_id,
        filename=file.filename,
        mime_type=mime_type,
        s3_key=s3_key,
        status="pending",
    )
    db.add(doc)
    await db.commit()
    await db.refresh(doc)

    # Enqueue ingestion job
    from src.lib.redis import get_arq_pool
    arq = await get_arq_pool()
    await arq.enqueue_job("ingest_document", document_id=str(document_id))

    return {"document": DocumentResponse.model_validate(doc)}

@router.get("", response_model=DocumentListResponse)
async def list_documents(
    chatbot_id: UUID,
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    auth: AuthenticatedTenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    # Verify chatbot
    chatbot = (await db.execute(
        select(Chatbot).where(tenant_where(Chatbot, auth.tenant_id, Chatbot.id == chatbot_id))
    )).scalar_one_or_none()
    if chatbot is None:
        raise NotFoundError("Chatbot not found")

    base = select(Document).where(tenant_where(Document, auth.tenant_id, Document.chatbot_id == chatbot_id))
    total = (await db.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
    rows = (await db.execute(
        base.order_by(Document.created_at.desc(), Document.id).limit(limit).offset(offset)
    )).scalars().all()

    return DocumentListResponse(
        items=[DocumentResponse.model_validate(r) for r in rows],
        total=total,
        has_more=total > offset + len(rows),
    )

@router.delete("/{document_id}", status_code=204)
async def delete_document(
    chatbot_id: UUID,
    document_id: UUID,
    auth: AuthenticatedTenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    doc = (await db.execute(
        select(Document).where(
            tenant_where(Document, auth.tenant_id, Document.id == document_id),
            Document.chatbot_id == chatbot_id,
        )
    )).scalar_one_or_none()
    if doc is None:
        raise NotFoundError()

    # Delete from S3
    async with s3_client() as s3:
        await s3.delete_object(Bucket=settings.S3_BUCKET, Key=doc.s3_key)

    # Hard-delete chunks, soft-delete document
    await db.execute(delete(Chunk).where(Chunk.document_id == document_id))
    doc.deleted_at = datetime.now(timezone.utc)
    await db.commit()
```

## ARQ pool helper (`api/src/lib/redis.py`)

```python
import redis.asyncio as aioredis
from arq.connections import ArqRedis, RedisSettings, create_pool
from src.config.env import settings

_arq_pool: ArqRedis | None = None

async def get_arq_pool() -> ArqRedis:
    global _arq_pool
    if _arq_pool is None:
        _arq_pool = await create_pool(RedisSettings.from_dsn(settings.REDIS_URL))
    return _arq_pool

redis_client = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
```

## Register in `main.py`

```python
from src.routes.documents import router as documents_router
api_v1.include_router(documents_router)
```

## Acceptance criteria

- Upload a PDF → file appears in MinIO, DB row with `status: pending`, ARQ job visible in Redis.
- File > 20MB → 413 `payload_too_large`.
- Unsupported MIME type → 415 `unsupported_media_type`.
- `GET /api/v1/chatbots/:id/documents` lists documents with status.
- `DELETE` removes S3 object, chunks, and soft-deletes the document row.
- Upload to a chatbot owned by another tenant → 404.
