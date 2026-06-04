import os
from fastapi import APIRouter, Depends, UploadFile, File, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete, func
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
    chatbot = (await db.execute(
        select(Chatbot).where(tenant_where(Chatbot, auth.tenant_id, Chatbot.id == chatbot_id))
    )).scalar_one_or_none()
    if chatbot is None:
        raise NotFoundError("Chatbot not found")

    mime_type = file.content_type or ""
    if mime_type not in ALLOWED_MIME_TYPES:
        raise UnsupportedMimeTypeError(mime_type)

    safe_filename = os.path.basename(file.filename or "upload").replace("\x00", "") or "upload"

    parts: list[bytes] = []
    size = 0
    while chunk := await file.read(65536):
        size += len(chunk)
        if size > MAX_FILE_SIZE:
            raise PayloadTooLargeError("File exceeds 20MB limit")
        parts.append(chunk)
    data = b"".join(parts)

    document_id = uuid4()
    s3_key = f"{auth.tenant_id}/{chatbot_id}/{document_id}/{safe_filename}"

    async with s3_client() as s3:
        await s3.put_object(
            Bucket=settings.S3_BUCKET,
            Key=s3_key,
            Body=data,
            ContentType=mime_type,
        )

    doc = Document(
        id=document_id,
        chatbot_id=chatbot_id,
        tenant_id=auth.tenant_id,
        filename=safe_filename,
        mime_type=mime_type,
        s3_key=s3_key,
        status="pending",
    )
    db.add(doc)
    await db.commit()
    await db.refresh(doc)

    from src.lib.redis import get_arq_pool
    arq = await get_arq_pool()
    await arq.enqueue_job("ingest_document", document_id=str(document_id))

    return {"document": DocumentResponse.model_validate(doc).model_dump(mode="json")}


@router.get("", response_model=DocumentListResponse)
async def list_documents(
    chatbot_id: UUID,
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    auth: AuthenticatedTenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    chatbot = (await db.execute(
        select(Chatbot).where(tenant_where(Chatbot, auth.tenant_id, Chatbot.id == chatbot_id))
    )).scalar_one_or_none()
    if chatbot is None:
        raise NotFoundError("Chatbot not found")

    base = select(Document).where(
        tenant_where(Document, auth.tenant_id, Document.chatbot_id == chatbot_id)
    )
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

    async with s3_client() as s3:
        await s3.delete_object(Bucket=settings.S3_BUCKET, Key=doc.s3_key)

    await db.execute(delete(Chunk).where(Chunk.document_id == document_id))
    doc.deleted_at = datetime.now(timezone.utc)
    await db.commit()
