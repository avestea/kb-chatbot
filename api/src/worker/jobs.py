import re
import uuid
from arq import Retry
from sqlalchemy import select, update
from src.db.base import async_session
from src.db.models import Document
from src.lib.s3 import s3_client
from src.lib.errors import UnsupportedMimeTypeError
from src.config.env import settings
from src.worker.parsers import get_parser_for
from src.lib.log import log


async def ingest_document(ctx: dict, document_id: str):
    """
    ARQ job: download from S3, parse to text, update status.
    Slice 6 extends this function to add chunking + embedding.
    """
    job_try = ctx.get("job_try", 1)

    async with async_session() as db:
        doc = (await db.execute(
            select(Document).where(Document.id == uuid.UUID(document_id))
        )).scalar_one_or_none()

        if doc is None:
            log.warning("document not found, acking", document_id=document_id)
            return

        doc.status = "processing"
        doc.error_reason = None
        await db.commit()

        try:
            async with s3_client() as s3:
                response = await s3.get_object(Bucket=settings.S3_BUCKET, Key=doc.s3_key)
                file_bytes = await response["Body"].read()

            parser = get_parser_for(doc.mime_type)
            text = await parser(file_bytes, doc.filename)
            text = _normalize_text(text)

            log.info("document parsed", document_id=document_id, text_length=len(text))

            # Slice 6 continues here with chunk + embed + persist
            # For now: leave status as 'processing' — Slice 6 sets it to 'ready'

        except UnsupportedMimeTypeError as exc:
            err_msg = str(exc)[:500]
            log.error("unsupported mime type", document_id=document_id, error=err_msg)
            async with async_session() as db2:
                await db2.execute(
                    update(Document)
                    .where(Document.id == uuid.UUID(document_id))
                    .values(status="error", error_reason=err_msg)
                )
                await db2.commit()

        except Exception as exc:
            err_msg = str(exc)[:500]
            log.error("ingest failed", document_id=document_id, error=err_msg, exc_info=exc)
            async with async_session() as db2:
                await db2.execute(
                    update(Document)
                    .where(Document.id == uuid.UUID(document_id))
                    .values(status="error", error_reason=err_msg)
                )
                await db2.commit()

            if job_try < 3:
                raise Retry(defer=5 ** job_try)


def _normalize_text(text: str) -> str:
    text = text.strip()
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text
