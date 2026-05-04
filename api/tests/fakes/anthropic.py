"""Scriptable fake for stream_completion — never calls the Anthropic API."""
import asyncio
from src.lib.llm import TokenEvent, UsageEvent


class FakeStreamCompletion:
    """
    Async generator callable that yields configured token/usage events.

    Usage:
        fake = FakeStreamCompletion(tokens=["Hello", " world"])
        with patch("src.routes.chat.stream_completion", new=fake):
            ...
    """

    def __init__(
        self,
        tokens: list[str] | None = None,
        input_tokens: int = 10,
        output_tokens: int = 20,
    ):
        self.tokens = tokens if tokens is not None else ["Hello", " world"]
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens

    async def __call__(
        self,
        *,
        system_prompt: str,
        messages: list,
        max_tokens: int = 1024,
        temperature: float = 0.2,
        cancel: asyncio.Event | None = None,
    ):
        for token in self.tokens:
            if cancel and cancel.is_set():
                raise asyncio.CancelledError()
            yield TokenEvent(text=token)
        yield UsageEvent(input_tokens=self.input_tokens, output_tokens=self.output_tokens)
