# Slice 13 — Query Rewriting

**Depends on:** Slice 8 (chat endpoint), Slice 12 (hybrid search)
**Prereq:** Read `00-prompt-prefix.md` first.

---

## Goal

Before embedding a user query, rewrite it into a self-contained question using the conversation history. This fixes retrieval for follow-up questions where the embedding has no context to grab onto.

| Turn | Raw query | Rewritten |
|---|---|---|
| 1 | "What is the refund policy?" | _(no rewrite needed — already self-contained)_ |
| 2 | "What about digital goods?" | "What is the refund policy for digital goods?" |
| 3 | "And subscriptions?" | "What is the refund policy for subscriptions?" |
| 4 | "Who do I contact?" | "Who do I contact to request a refund?" |

One small LLM call (Haiku, ~50 tokens) before every retrieval step. The full Claude answer is unchanged.

---

## How it works

```
User message
    │
    ▼
rewrite_query(message, history)   ← new step, 1 LLM call
    │
    ▼
retrieve_context(query=rewritten_query)
    │
    ▼
stream_completion(...)            ← receives original message, not rewritten
```

The rewritten query is used **only for retrieval**. The LLM still sees the original message and full conversation history — so the answer reads naturally.

---

## New file: `api/src/rag/rewrite.py`

```python
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
```

---

## Chat endpoint changes (`api/src/routes/chat.py`)

Import and call `rewrite_query` before retrieval:

```python
from src.rag.rewrite import rewrite_query

# Inside _generate(), after loading conversation history, before retrieve_context():

# Build history list from prior messages in this conversation
prior_messages = [
    {"role": m.role, "content": m.content}
    for m in existing_messages   # loaded earlier in the function
]

retrieval_query = await rewrite_query(message, prior_messages)

chunks = await retrieve_context(
    chatbot_id=chatbot_id,
    query=retrieval_query,     # ← rewritten query
    top_k=5,
    min_similarity=0.75,
)
```

The `messages` list passed to `stream_completion` still uses the **original** `message` — the rewrite is invisible to the LLM and the end user.

---

## Keeping the rewrite observable (optional)

Add `rewritten_query` to the `meta` SSE event so you can see in the sources panel what was actually searched:

```python
yield sse("meta", {
    "conversation_id": str(conversation.id),
    "source_count": len(chunks),
    "retrieval_query": retrieval_query,   # omit if same as original
})
```

In `api_client.py`, capture it from the `meta` event if present:
```python
if current_event == "meta":
    meta = data
    retrieval_query = data.get("retrieval_query", "")
```

Display it in the Gradio sources panel as a grey subtitle: `"Searched for: {retrieval_query}"`.

---

## Cost and latency

- **Model**: `claude-haiku-4-5-20251001` — cheapest Claude model.
- **Token usage**: ~60 input + ~30 output = ~90 tokens per query rewrite.
- **Latency**: ~100–200 ms. Runs before retrieval, so it adds to time-to-first-token but not to the streaming portion the user perceives.
- **Skip condition**: if `not history`, no API call is made (synchronous early return).

---

## No schema changes

No new tables, columns, or migrations. `rewrite_query` is a pure function from `(str, list) → str`.

---

## Acceptance criteria

- Turn 1 query (no history) → `rewrite_query` returns the original message unchanged.
- Turn 2+ with a follow-up like "And for digital goods?" → retrieval uses a self-contained query like "What is the refund policy for digital goods?"
- LLM sees original message + full history (not the rewritten query).
- If `rewrite_query` raises any exception, the original message is used for retrieval — chat never breaks.
- No changes needed to Slices 7, 10, 11, or 12.
