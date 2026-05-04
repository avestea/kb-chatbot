from anthropic import AsyncAnthropic
from src.config.env import settings

_client = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)

REWRITE_SYSTEM = (
    "You are a search query optimizer. "
    "Given a conversation history and a new user message, "
    "rewrite the message into a single, self-contained search query "
    "that includes all context needed to retrieve the relevant information. "
    "Output ONLY the rewritten query — no explanation, no punctuation changes beyond what is needed."
)


async def rewrite_query(message: str, history: list[dict]) -> str:
    """
    Rewrites `message` into a self-contained retrieval query using prior turns.
    Returns the original message unchanged if history is empty or rewriting fails.
    history items: {"role": "user"|"assistant", "content": str}
    """
    if not history:
        return message

    # Build a compact transcript (last 6 turns max to stay under ~200 tokens)
    recent = history[-6:]
    transcript_lines = [f"{m['role'].upper()}: {m['content']}" for m in recent]
    transcript = "\n".join(transcript_lines)

    prompt = (
        f"Conversation so far:\n{transcript}\n\n"
        f"New message: {message}\n\n"
        "Rewritten query:"
    )

    try:
        response = await _client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            system=REWRITE_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        rewritten = response.content[0].text.strip()
        return rewritten if rewritten else message
    except Exception:
        # Rewriting is best-effort — fall back to original on any error
        return message
