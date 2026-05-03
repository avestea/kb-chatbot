# Slice 8 — Chat Endpoint (SSE + LLM Streaming)

**Depends on:** Slice 1, Slice 3, Slice 7
**Prereq:** Read `00-prompt-prefix.md` first.

---

## Goal

End users can ask questions and receive grounded streaming answers. Conversation and message rows are persisted.

## Deliverables

- `api/src/lib/llm.py` — `stream_completion()` Anthropic wrapper (locked contract).
- `api/src/rag/prompt.py` — system prompt assembler + `PROMPT_VERSION` constant.
- `api/src/schemas/chat.py` — request schema.
- `api/src/routes/chat.py` — `POST /api/v1/chat/{chatbot_id}/message` SSE endpoint.
- `api/src/config/chat.py` — tunable constants.
- Register router with wildcard CORS in `main.py`.

## Config (`api/src/config/chat.py`)

```python
MAX_MESSAGE_TOKENS = 4000
MAX_CONTEXT_TURNS = 10       # prior conversation turns passed to LLM (Slice 8+)
MAX_PROMPT_TOKENS = 12000    # token budget guard
```

## Anthropic wrapper (`api/src/lib/llm.py`)

```python
import asyncio
from dataclasses import dataclass, field
from typing import AsyncIterator
from anthropic import AsyncAnthropic
from src.config.env import settings

@dataclass
class ChatTurn:
    role: str   # 'user' | 'assistant'
    content: str

@dataclass
class TokenEvent:
    type: str = 'token'
    text: str = ''

@dataclass
class UsageEvent:
    type: str = 'usage'
    input_tokens: int = 0
    output_tokens: int = 0

StreamEvent = TokenEvent | UsageEvent

_client = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)

async def stream_completion(
    *,
    system_prompt: str,
    messages: list[ChatTurn],
    max_tokens: int = 1024,
    temperature: float = 0.2,
    cancel: asyncio.Event | None = None,
) -> AsyncIterator[StreamEvent]:
    """
    Yields TokenEvent(s) then one final UsageEvent.
    If cancel is set: stops yielding and raises asyncio.CancelledError.
    """
    anthropic_messages = [{"role": m.role, "content": m.content} for m in messages]

    async with _client.messages.stream(
        model="claude-sonnet-4-6",
        system=system_prompt,
        messages=anthropic_messages,
        max_tokens=max_tokens,
        temperature=temperature,
    ) as stream:
        async for text in stream.text_stream:
            if cancel and cancel.is_set():
                raise asyncio.CancelledError()
            yield TokenEvent(text=text)

        usage = (await stream.get_final_message()).usage
        yield UsageEvent(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
        )
```

## Prompt assembler (`api/src/rag/prompt.py`)

```python
from src.rag.retrieve import RetrievedChunk

PROMPT_VERSION = "v1"

SYSTEM_TEMPLATE = """\
You are a helpful assistant for {chatbot_name}.
Answer questions based ONLY on the following context retrieved from the knowledge base.
If the answer is not found in the context, say "I don't have information about that in my knowledge base."
Do not make up information. Be concise and direct.

Context:
{context}
"""

def build_system_prompt(chatbot_name: str, chunks: list[RetrievedChunk]) -> str:
    if chunks:
        context = "\n".join(
            f"[{i+1}] ({c.document_name}) {c.content}"
            for i, c in enumerate(chunks)
        )
    else:
        context = "(no relevant context found)"
    return SYSTEM_TEMPLATE.format(chatbot_name=chatbot_name, context=context)
```

## Request schema (`api/src/schemas/chat.py`)

```python
from pydantic import BaseModel, Field

class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000)
    session_id: str = Field(..., min_length=1, max_length=128)
```

## Chat route (`api/src/routes/chat.py`)

```python
import asyncio
import json
import uuid
from datetime import datetime, timezone
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select, insert
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

    # Heartbeat to keep connection alive through reverse proxies
    heartbeat_task = asyncio.create_task(_heartbeat(request))

    try:
        async with async_session() as db:
            # Validate chatbot exists (no auth — public endpoint)
            chatbot = (await db.execute(
                select(Chatbot).where(
                    Chatbot.id == chatbot_id,
                    Chatbot.deleted_at.is_(None),
                )
            )).scalar_one_or_none()
            if chatbot is None:
                yield sse("error", {"message": "Chatbot not found"})
                return

            # Upsert conversation (race-safe via unique constraint)
            stmt = pg_insert(Conversation).values(
                chatbot_id=chatbot_id,
                session_id=body.session_id,
            ).on_conflict_do_update(
                index_elements=["chatbot_id", "session_id"],
                set_={"session_id": body.session_id},
            ).returning(Conversation.id)
            conv_id = (await db.execute(stmt)).scalar_one()
            await db.commit()

            # Persist user message
            user_msg = Message(
                conversation_id=conv_id,
                role="user",
                content=body.message,
            )
            db.add(user_msg)
            await db.commit()

        # Retrieve context
        chunks = await retrieve_context(chatbot_id=chatbot_id, query=body.message)

        yield sse("meta", {
            "conversation_id": str(conv_id),
            "source_count": len(chunks),
        })

        system_prompt = build_system_prompt(chatbot.name, chunks)
        messages = [ChatTurn(role="user", content=body.message)]

        # Stream from Claude
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

        # Persist assistant message
        no_answer = "I don't have information about that" in assistant_text
        async with async_session() as db:
            assistant_msg = Message(
                conversation_id=conv_id,
                role="assistant",
                content=assistant_text,
                source_chunk_ids=[c.id for c in chunks],
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

async def _heartbeat(request: Request):
    """Send SSE comments every 15s to keep reverse proxies alive."""
    # Note: this needs to write to the response stream.
    # In practice, implement as a periodic yield in _generate using asyncio.
    # This stub is a placeholder — wire it into the generator loop.
    try:
        while True:
            await asyncio.sleep(15)
    except asyncio.CancelledError:
        pass
```

> **Heartbeat note:** The standalone `_heartbeat` task pattern doesn't work with a generator — you can't write from a separate task. Instead, use `asyncio.wait_for` with a short timeout in the streaming loop to interleave `: heartbeat\n\n` comment lines. Or simply skip heartbeats for MVP and ensure your reverse proxy has a long enough idle timeout.

## Register in `main.py` with wildcard CORS

The chat endpoint is public — it must allow `*` origins (embedded chatbots run on any customer site).

```python
from fastapi.middleware.cors import CORSMiddleware
from fastapi import FastAPI
from starlette.middleware.base import BaseHTTPMiddleware

# For the chat route only, override CORS:
# Simplest approach for MVP: allow all origins globally.
# Tighten per-route CORS after MVP if needed.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["*"],
)

from src.routes.chat import router as chat_router
api_v1.include_router(chat_router)
```

## Acceptance criteria

- Ask a question answered in an uploaded doc → streamed answer is grounded; `source_chunk_ids` on persisted message reference retrieved chunks.
- Ask an off-topic question → response is "I don't have information about that in my knowledge base."
- SSE event order: `meta` → `token...` → `done`.
- `tokensUsed` is populated on the assistant message.
- `no_answer` flag is `true` when the fallback phrase appears in the response.
- Disconnecting the client mid-stream does not cause an unhandled exception in the server.
- Chatbot ID for a deleted chatbot → `event: error` (no crash).
