# Slice 10 — Explainability (Source Attribution)

**Depends on:** Slice 8 (chat endpoint), Slice 9 (Gradio UI)
**Prereq:** Read `00-prompt-prefix.md` first.

---

## Goal

Every chat answer shows which document chunks it drew from, with similarity scores and the actual text snippet. Users stop treating the chatbot as a black box.

The data is already in the system — `source_chunk_ids` is stored on every `Message`, and `retrieve_context` already returns similarity scores. This slice is mostly wiring and UI.

---

## Backend changes

### 1. Add a `sources` SSE event (`api/src/routes/chat.py`)

Extend `_generate()` to emit a `sources` event between `meta` and the first `token`. The payload carries enough for the UI to render a source panel without a second API call.

```python
# After yielding the meta event, before streaming tokens:

sources_payload = [
    {
        "index": i + 1,
        "chunk_id": c.id,
        "document_name": c.document_name,
        "snippet": c.content[:300],   # first 300 chars is enough for a preview
        "similarity": round(c.similarity, 3),
    }
    for i, c in enumerate(chunks)
]
yield sse("sources", {"sources": sources_payload})
```

**Updated SSE event order:**
```
event: meta     data: {"conversation_id": "...", "source_count": 3}
event: sources  data: {"sources": [{...}, {...}, {...}]}
event: token    data: {"text": "..."}
...
event: done     data: {"message_id": "..."}
```

### 2. Store similarity scores on the message (`api/src/db/models.py`)

The existing `source_chunk_ids` JSONB column only stores IDs. Extend it to store the full source objects so the evaluation dashboard (Slice 11) can retrieve them without re-querying chunks.

Add a migration:

```sql
ALTER TABLE messages ADD COLUMN source_chunks jsonb;
```

`source_chunks` stores the same payload as the `sources` SSE event:
```json
[
  {"index": 1, "chunk_id": "...", "document_name": "policy.pdf",
   "snippet": "Customers may request...", "similarity": 0.89}
]
```

Update the chat route to persist this:

```python
assistant_msg = Message(
    ...
    source_chunk_ids=[c["chunk_id"] for c in sources_payload],
    source_chunks=sources_payload,    # new field
    ...
)
```

Add to `models.py`:
```python
source_chunks: Mapped[Optional[list[dict]]] = mapped_column(JSONB)
```

---

## Gradio UI changes (`web/app.py` + `web/api_client.py`)

### 1. Parse the `sources` event in `api_client.py`

`chat_stream` currently yields only text deltas. Extend it to also yield source data via a callback:

```python
def chat_stream(
    self,
    chatbot_id: str,
    message: str,
    session_id: str,
    on_sources=None,   # callable(list[dict]) — called once when sources arrive
) -> Iterator[str]:
    import json as _json
    with httpx.Client(base_url=API_BASE, timeout=120) as c:
        with c.stream(
            "POST",
            f"/api/v1/chat/{chatbot_id}/message",
            json={"message": message, "session_id": session_id},
        ) as response:
            response.raise_for_status()
            buffer = ""
            for line in response.iter_lines():
                if not line.startswith("data: "):
                    continue
                try:
                    event_line = line  # peek at previous line for event name
                    data = _json.loads(line[6:])
                except _json.JSONDecodeError:
                    continue

                if "sources" in data and on_sources:
                    on_sources(data["sources"])
                elif "text" in data:
                    buffer += data["text"]
                    yield buffer
```

> Because SSE lines come as `event: X\ndata: {...}\n\n`, you need to track the preceding `event:` line to know which type each `data:` belongs to. Use a two-line buffer:

```python
def chat_stream(self, chatbot_id, message, session_id, on_sources=None):
    import json as _json
    with httpx.Client(base_url=API_BASE, timeout=120) as c:
        with c.stream("POST", f"/api/v1/chat/{chatbot_id}/message",
                      json={"message": message, "session_id": session_id}) as resp:
            resp.raise_for_status()
            current_event = ""
            buffer = ""
            for line in resp.iter_lines():
                if line.startswith("event: "):
                    current_event = line[7:].strip()
                elif line.startswith("data: "):
                    try:
                        data = _json.loads(line[6:])
                    except _json.JSONDecodeError:
                        continue
                    if current_event == "sources" and on_sources:
                        on_sources(data.get("sources", []))
                    elif current_event == "token" and "text" in data:
                        buffer += data["text"]
                        yield buffer
```

### 2. Sources panel in the Chat tab

Add a `gr.JSON` component below the chatbot that updates when sources arrive:

```python
with gr.Tab("Chat"):
    chatbot_select_chat = gr.Dropdown(label="Select Chatbot", choices=[])
    chat_display = gr.Chatbot(label="Conversation", height=400)
    sources_display = gr.JSON(label="Sources used", visible=False)

    with gr.Row():
        msg_input = gr.Textbox(label="Message", scale=4)
        send_btn = gr.Button("Send", variant="primary", scale=1)
```

Update the chat handler to capture and display sources:

```python
def chat_handler(message, history, token, chatbot_id, session_id_state):
    if not chatbot_id:
        yield history + [[message, "Select a chatbot first."]], None, gr.update(visible=False)
        return

    session_id = session_id_state or str(uuid.uuid4())
    sources_captured = []

    def capture_sources(sources):
        sources_captured.extend(sources)

    partial = ""
    for chunk in get_client(token).chat_stream(
        chatbot_id, message, session_id, on_sources=capture_sources
    ):
        partial = chunk
        yield (
            history + [[message, partial]],
            session_id,
            gr.update(visible=False),   # hide sources while streaming
        )

    # Final yield with sources visible
    yield (
        history + [[message, partial]],
        session_id,
        gr.update(value=sources_captured, visible=bool(sources_captured)),
    )

send_btn.click(
    chat_handler,
    inputs=[msg_input, chat_display, token_input, chatbot_id_state, session_id_state],
    outputs=[chat_display, session_id_state, sources_display],
).then(lambda: "", outputs=[msg_input])
```

### What the sources panel shows

`gr.JSON` renders the sources payload as a collapsible tree:
```json
[
  {
    "index": 1,
    "document_name": "refund-policy.pdf",
    "similarity": 0.89,
    "snippet": "Customers may request a refund within 30 days of purchase..."
  },
  {
    "index": 2,
    "document_name": "faq.html",
    "similarity": 0.81,
    "snippet": "Refunds are processed within 5–7 business days..."
  }
]
```

For a richer display, replace `gr.JSON` with a `gr.Dataframe`:
```python
sources_display = gr.Dataframe(
    headers=["#", "Document", "Similarity", "Snippet"],
    visible=False,
)

# In the final yield:
rows = [[s["index"], s["document_name"], s["similarity"], s["snippet"]] for s in sources_captured]
gr.update(value=rows, visible=bool(rows))
```

---

## Migration

```bash
docker compose exec api alembic revision --autogenerate -m "add_source_chunks_to_messages"
docker compose exec api alembic upgrade head
```

The `source_chunks` column defaults to NULL for existing rows — no backfill needed.

---

## Acceptance criteria

- Chat response SSE stream includes a `sources` event before any `token` event.
- Sources panel in Gradio shows document names, similarity scores, and snippets after each answer.
- Panel is hidden while the answer is streaming; appears after `done`.
- Off-topic question (no chunks retrieved) → panel is hidden / empty.
- `messages.source_chunks` is populated for every assistant message.
- Similarity scores are between 0 and 1; highest score is listed first.
