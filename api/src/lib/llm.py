import asyncio
import uuid
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
    type: str = field(default='token')
    text: str = field(default='')


@dataclass
class UsageEvent:
    type: str = field(default='usage')
    input_tokens: int = field(default=0)
    output_tokens: int = field(default=0)
    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))


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
    If cancel is set mid-stream: stops yielding and raises asyncio.CancelledError.
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
