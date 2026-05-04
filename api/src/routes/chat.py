import asyncio
import json
import uuid
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.db.base import async_session
from src.db.models import Chatbot, Conversation, Message
from src.lib.llm import stream_completion, ChatTurn
from src.rag.retrieve import retrieve_context
from src.rag.prompt import build_system_prompt, PROMPT_VERSION
from src.schemas.chat import ChatRequest
from src.lib.log import log

router = APIRouter(prefix="/chat", tags=["chat"])


@router.post("/{chatbot_id}/message")
async def chat_message(chatbot_id: str, body: ChatRequest, request: Request):
    return StreamingResponse(
        _generate(chatbot_id, body, request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def _generate(chatbot_id: str, body: ChatRequest, request: Request):
    def sse(event: str, data: dict) -> str:
        return f"event: {event}\ndata: {json.dumps(data)}\n\n"

    cancel = asyncio.Event()
    heartbeat_task = asyncio.create_task(_heartbeat())

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
                chatbot_id=uuid.UUID(chatbot_id),
                session_id=body.session_id,
            ).on_conflict_do_update(
                constraint="conversations_chatbot_session_uq",
                set_={"session_id": body.session_id},
            ).returning(Conversation.id)
            conv_id = (await db.execute(stmt)).scalar_one()
            await db.commit()

            user_msg = Message(
                conversation_id=conv_id,
                role="user",
                content=body.message,
            )
            db.add(user_msg)
            await db.commit()

        chunks = await retrieve_context(chatbot_id=chatbot_id, query=body.message)

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

        yield sse("meta", {
            "conversation_id": str(conv_id),
            "source_count": len(chunks),
        })

        if sources_payload:
            yield sse("sources", {"sources": sources_payload})

        system_prompt = build_system_prompt(chatbot.name, chunks)
        messages = [ChatTurn(role="user", content=body.message)]

        assistant_text = ""
        input_tokens = 0
        output_tokens = 0

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

        no_answer = "I don't have information about that" in assistant_text
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
