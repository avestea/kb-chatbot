import asyncio
import json
import uuid
from uuid import UUID
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.db.base import async_session
from src.db.models import Chatbot, Conversation, Message
from src.lib.llm import stream_completion, ChatTurn
from src.lib.observability import RequestContext, log_request, measure_latency
from src.lib.redis import get_redis_client
from src.lib.errors import RateLimitError
from src.rag.retrieve import retrieve_context
from src.rag.rewrite import rewrite_query, count_rewrite_tokens
from src.rag.prompt import build_system_prompt, PROMPT_VERSION
from src.schemas.chat import ChatRequest
from src.lib.log import log

router = APIRouter(prefix="/chat", tags=["chat"])

_RATE_LIMIT_REQUESTS = 60
_RATE_LIMIT_WINDOW = 60


async def _check_rate_limit(chatbot_id: UUID, request: Request) -> None:
    try:
        client = await get_redis_client()
        forwarded = request.headers.get("X-Forwarded-For")
        ip = forwarded.split(",")[0].strip() if forwarded else (
            request.client.host if request.client else "unknown"
        )
        key = f"rate:chat:{chatbot_id}:{ip}"
        count = await client.incr(key)
        if count == 1:
            await client.expire(key, _RATE_LIMIT_WINDOW)
        if count > _RATE_LIMIT_REQUESTS:
            raise RateLimitError(f"Too many requests. Retry after {_RATE_LIMIT_WINDOW} seconds.")
    except RateLimitError:
        raise
    except Exception:
        pass  # fail open if Redis is unavailable


@router.post("/{chatbot_id}/message")
async def chat_message(chatbot_id: UUID, body: ChatRequest, request: Request):
    await _check_rate_limit(chatbot_id, request)
    return StreamingResponse(
        _generate(chatbot_id, body, request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def _generate(chatbot_id: UUID, body: ChatRequest, request: Request):
    def sse(event: str, data: dict) -> str:
        return f"event: {event}\ndata: {json.dumps(data)}\n\n"

    cancel = asyncio.Event()
    heartbeat_task = asyncio.create_task(_heartbeat())
    request_ctx = RequestContext.new()

    try:
        async with async_session() as db:
            chatbot = (await db.execute(
                select(Chatbot).where(
                    Chatbot.id == chatbot_id,
                    Chatbot.deleted_at.is_(None),
                )
            )).scalar_one_or_none()
            if chatbot is None:
                yield sse("error", {"message": "Chatbot not found"})
                return

            stmt = pg_insert(Conversation).values(
                chatbot_id=chatbot_id,
                session_id=body.session_id,
            ).on_conflict_do_update(
                constraint="conversations_chatbot_session_uq",
                set_={"session_id": body.session_id},
            ).returning(Conversation.id)
            conv_id = (await db.execute(stmt)).scalar_one()
            await db.commit()

            # Load prior messages for query rewriting (before inserting current message)
            prior_rows = (await db.execute(
                select(Message)
                .where(Message.conversation_id == conv_id)
                .order_by(Message.created_at)
            )).scalars().all()
            prior_history = [{"role": m.role, "content": m.content} for m in prior_rows]

            user_msg = Message(
                conversation_id=conv_id,
                role="user",
                content=body.message,
            )
            db.add(user_msg)
            await db.commit()

        # Query rewriting (turns >= 2)
        rewrite_ctx = RequestContext.new()
        rewrite_tokens = count_rewrite_tokens(body.message, prior_history) if prior_history else 0
        try:
            async with measure_latency() as lat:
                retrieval_query = await rewrite_query(
                    body.message, prior_history,
                    request_context=rewrite_ctx,
                )
            rewrite_latency = lat()
        except Exception:
            retrieval_query = body.message
            rewrite_latency = 0

        if retrieval_query != body.message:
            async with async_session() as log_db:
                await log_request(
                    log_db,
                    tenant_id=str(chatbot.tenant_id),
                    chatbot_id=str(chatbot.id),
                    provider="anthropic",
                    model="claude-haiku-4-5-20251001",
                    phase="chat_rewrite",
                    direction="input",
                    tokens=rewrite_tokens,
                    latency_ms=rewrite_latency,
                    request_context=rewrite_ctx,
                )
                await log_db.commit()

        chunks = await retrieve_context(
            chatbot_id=str(chatbot_id), query=retrieval_query,
            request_context=request_ctx,
            tenant_id=str(chatbot.tenant_id),
        )

        sources_payload = [
            {
                "index": i + 1,
                "chunk_id": c.id,
                "document_name": c.document_name,
                "snippet": c.content[:300],
                "similarity": round(c.similarity, 3),
                "match_type": "keyword" if c.similarity == 0.0 else "semantic",
            }
            for i, c in enumerate(chunks)
        ]

        meta_data: dict = {
            "conversation_id": str(conv_id),
            "source_count": len(chunks),
        }
        if retrieval_query != body.message:
            meta_data["retrieval_query"] = retrieval_query
        yield sse("meta", meta_data)

        if sources_payload:
            yield sse("sources", {"sources": sources_payload})

        system_prompt = build_system_prompt(chatbot.name, chunks)
        messages = [ChatTurn(role="user", content=body.message)]

        assistant_text = ""
        input_tokens = 0
        output_tokens = 0

        async with measure_latency() as lat:
            async for event in stream_completion(
                system_prompt=system_prompt,
                messages=messages,
                max_tokens=1024,
                temperature=0.2,
                cancel=cancel,
            ):
                if await request.is_disconnected():
                    cancel.set()
                    break
                if event.type == "token":
                    assistant_text += event.text
                    yield sse("token", {"text": event.text})
                elif event.type == "usage":
                    input_tokens = event.input_tokens
                    output_tokens = event.output_tokens
            chat_latency = lat()

        no_answer = "I don't have information about that" in assistant_text

        # Persist assistant message
        async with async_session() as db:
            assistant_msg = Message(
                conversation_id=conv_id,
                role="assistant",
                content=assistant_text,
                source_chunk_ids=[c["chunk_id"] for c in sources_payload],
                source_chunks=sources_payload if sources_payload else None,
                tokens_used=input_tokens + output_tokens,
                no_answer=no_answer,
                prompt_version=PROMPT_VERSION,
            )
            db.add(assistant_msg)
            await db.commit()
            await db.refresh(assistant_msg)

        # Log the chat_response call (both directions in one transaction)
        async with async_session() as log_db:
            await log_request(
                log_db,
                tenant_id=str(chatbot.tenant_id),
                chatbot_id=str(chatbot.id),
                provider="anthropic",
                model="claude-sonnet-4-6",
                phase="chat_response",
                direction="input",
                tokens=input_tokens,
                latency_ms=chat_latency,
                request_context=request_ctx,
            )
            await log_request(
                log_db,
                tenant_id=str(chatbot.tenant_id),
                chatbot_id=str(chatbot.id),
                provider="anthropic",
                model="claude-sonnet-4-6",
                phase="chat_response",
                direction="output",
                tokens=output_tokens,
                latency_ms=chat_latency,
                request_context=request_ctx,
            )
            await log_db.commit()

        yield sse("done", {"message_id": str(assistant_msg.id)})

    except asyncio.CancelledError:
        log.info("chat stream cancelled by client", chatbot_id=chatbot_id)
    except Exception as exc:
        log.error("chat stream error", exc_info=exc)
        yield sse("error", {"message": "Generation failed"})
    finally:
        heartbeat_task.cancel()


async def _heartbeat():
    """Cancelled by _generate's finally block; effectively a no-op for MVP."""
    try:
        while True:
            await asyncio.sleep(15)
    except asyncio.CancelledError:
        pass
