import re
import uuid
from arq import Retry
from sqlalchemy import select, update, delete
from src.db.base import async_session
from src.db.models import Document, Chunk
from src.lib.s3 import s3_client
from src.lib.errors import UnsupportedMimeTypeError
from src.config.env import settings
from src.worker.parsers import get_parser_for
from src.worker.chunker import chunk_text
from src.lib.embedder import embed_chunks, EMBEDDING_MODEL
from src.lib.log import log


async def ingest_document(ctx: dict, document_id: str):
    """ARQ job: download from S3, parse, chunk, embed, persist chunks, mark ready."""
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
            # 1. Download from S3
            async with s3_client() as s3:
                response = await s3.get_object(Bucket=settings.S3_BUCKET, Key=doc.s3_key)
                file_bytes = await response["Body"].read()

            # 2. Parse
            parser = get_parser_for(doc.mime_type)
            text = _normalize_text(await parser(file_bytes, doc.filename))

            log.info("document parsed", document_id=document_id, text_length=len(text))

            # 3. Chunk
            draft_chunks = chunk_text(text)
            if not draft_chunks:
                doc.status = "error"
                doc.error_reason = "document produced no chunks"
                await db.commit()
                return

            # 4. Idempotency: delete prior chunks before inserting new ones
            await db.execute(delete(Chunk).where(Chunk.document_id == doc.id))
            await db.commit()

            # 5. Embed + insert in batches of 100
            BATCH = 100
            for i in range(0, len(draft_chunks), BATCH):
                batch = draft_chunks[i:i + BATCH]
                embeddings = await embed_chunks([c.content for c in batch])
                db.add_all([
                    Chunk(
                        document_id=doc.id,
                        chatbot_id=doc.chatbot_id,
                        tenant_id=doc.tenant_id,
                        content=c.content,
                        token_count=c.token_count,
                        chunk_index=c.chunk_index,
                        embedding=emb,
                        embedding_model=EMBEDDING_MODEL,
                    )
                    for c, emb in zip(batch, embeddings)
                ])
                await db.commit()

            # 6. Mark ready
            doc.status = "ready"
            doc.error_reason = None
            await db.commit()
            log.info("document ingested", document_id=document_id, chunks=len(draft_chunks))

        except UnsupportedMimeTypeError as exc:
            err_msg = str(exc)[:500]
            log.error("unsupported mime type", document_id=document_id, error=err_msg)
            async with async_session() as db2:
                await db2.execute(delete(Chunk).where(Chunk.document_id == uuid.UUID(document_id)))
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
                await db2.execute(delete(Chunk).where(Chunk.document_id == uuid.UUID(document_id)))
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
