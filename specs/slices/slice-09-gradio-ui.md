# Slice 9 — Gradio UI

**Depends on:** Slice 8
**Prereq:** Read `00-prompt-prefix.md` first.

---

## Goal

A Python-only UI that replaces both the Next.js dashboard and the embeddable widget. Customers can manage chatbots, upload documents, and chat — all without leaving Python.

## Tech

- **Gradio** — Python UI framework. Runs on port 7860. No JavaScript required.
- **httpx** — async HTTP client for calling the FastAPI backend.
- The Gradio app is a separate Docker service that calls `http://api:8000`.

## Deliverables

- `web/requirements.txt`
- `web/app.py` — Gradio multi-tab application.
- `web/api_client.py` — typed thin wrapper around the FastAPI REST API.
- `web/Dockerfile`

## `web/requirements.txt`

```
gradio>=5.0.0
httpx>=0.28.0
python-dotenv>=1.0.0
```

## API client (`web/api_client.py`)

```python
import httpx
import os
from typing import Iterator

API_BASE = os.getenv("API_BASE_URL", "http://api:8000")

class APIClient:
    def __init__(self, token: str):
        self.token = token
        self.headers = {"Authorization": f"Bearer {token}"}

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=API_BASE, headers=self.headers, timeout=30)

    async def list_chatbots(self) -> list[dict]:
        async with self._client() as c:
            r = await c.get("/api/v1/chatbots")
            r.raise_for_status()
            return r.json()["items"]

    async def create_chatbot(self, name: str, system_prompt: str | None = None) -> dict:
        async with self._client() as c:
            r = await c.post("/api/v1/chatbots", json={
                "name": name,
                "system_prompt_override": system_prompt or None,
            })
            r.raise_for_status()
            return r.json()["chatbot"]

    async def list_documents(self, chatbot_id: str) -> list[dict]:
        async with self._client() as c:
            r = await c.get(f"/api/v1/chatbots/{chatbot_id}/documents")
            r.raise_for_status()
            return r.json()["items"]

    async def upload_document(self, chatbot_id: str, file_path: str, filename: str, mime: str) -> dict:
        async with self._client() as c:
            with open(file_path, "rb") as f:
                r = await c.post(
                    f"/api/v1/chatbots/{chatbot_id}/documents",
                    files={"file": (filename, f, mime)},
                )
            r.raise_for_status()
            return r.json()["document"]

    async def delete_document(self, chatbot_id: str, document_id: str) -> None:
        async with self._client() as c:
            r = await c.delete(f"/api/v1/chatbots/{chatbot_id}/documents/{document_id}")
            r.raise_for_status()

    def chat_stream(self, chatbot_id: str, message: str, session_id: str) -> Iterator[str]:
        """Synchronous generator — Gradio streaming requires sync generators."""
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
                    if line.startswith("data: "):
                        try:
                            data = _json.loads(line[6:])
                            if "text" in data:
                                buffer += data["text"]
                                yield buffer  # Gradio streaming: yield cumulative text
                        except _json.JSONDecodeError:
                            pass
```

## Gradio app (`web/app.py`)

```python
import gradio as gr
import asyncio
import os
import uuid
from web.api_client import APIClient

# ─── Helpers ────────────────────────────────────────────────────────────────

def get_client(token: str) -> APIClient:
    if not token.strip():
        raise gr.Error("Enter your API token first.")
    return APIClient(token.strip())

def run(coro):
    """Run an async coroutine from synchronous Gradio handler."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(asyncio.run, coro)
                return future.result()
    except RuntimeError:
        pass
    return asyncio.run(coro)

# ─── Tab: Chatbots ───────────────────────────────────────────────────────────

def refresh_chatbots(token):
    bots = run(get_client(token).list_chatbots())
    rows = [[b["id"], b["name"], b["created_at"]] for b in bots]
    choices = {b["name"]: b["id"] for b in bots}
    return (
        gr.Dataframe(value=rows, headers=["ID", "Name", "Created"]),
        gr.Dropdown(choices=list(choices.keys()), label="Select chatbot"),
        choices,
    )

def create_chatbot_handler(token, name, system_prompt):
    if not name.strip():
        raise gr.Error("Name is required.")
    bot = run(get_client(token).create_chatbot(name.strip(), system_prompt.strip() or None))
    return f"Created: {bot['name']} ({bot['id']})"

# ─── Tab: Documents ──────────────────────────────────────────────────────────

def refresh_documents(token, chatbot_id):
    if not chatbot_id:
        return gr.Dataframe(value=[], headers=["ID", "Filename", "Status", "Error"])
    docs = run(get_client(token).list_documents(chatbot_id))
    rows = [[d["id"], d["filename"], d["status"], d.get("error_reason", "")] for d in docs]
    return gr.Dataframe(value=rows, headers=["ID", "Filename", "Status", "Error"])

def upload_handler(token, chatbot_id, file):
    if not chatbot_id:
        raise gr.Error("Select a chatbot first.")
    if file is None:
        raise gr.Error("No file selected.")
    import mimetypes
    mime, _ = mimetypes.guess_type(file.name)
    mime = mime or "application/octet-stream"
    filename = os.path.basename(file.name)
    doc = run(get_client(token).upload_document(chatbot_id, file.name, filename, mime))
    return f"Uploaded: {doc['filename']} — status: {doc['status']}"

# ─── Tab: Chat ───────────────────────────────────────────────────────────────

def chat_handler(message, history, token, chatbot_id, session_id_state):
    if not chatbot_id:
        yield history + [[message, "Select a chatbot first."]]
        return
    session_id = session_id_state or str(uuid.uuid4())

    partial = ""
    for chunk in get_client(token).chat_stream(chatbot_id, message, session_id):
        partial = chunk
        yield history + [[message, partial]]

    yield history + [[message, partial]]
    return session_id  # update session state

# ─── Layout ──────────────────────────────────────────────────────────────────

with gr.Blocks(title="KB Chatbot", theme=gr.themes.Soft()) as demo:
    gr.Markdown("# Knowledge Base Chatbot")
    token_input = gr.Textbox(label="API Token", type="password", placeholder="devtest:your-clerk-id")
    chatbot_id_state = gr.State("")
    session_id_state = gr.State("")
    chatbot_choices_state = gr.State({})

    with gr.Tab("Chatbots"):
        with gr.Row():
            refresh_btn = gr.Button("Refresh", variant="secondary")
        chatbot_table = gr.Dataframe(headers=["ID", "Name", "Created"])
        gr.Markdown("### Create New Chatbot")
        with gr.Row():
            new_name = gr.Textbox(label="Name", placeholder="My Chatbot")
            new_prompt = gr.Textbox(label="System Prompt (optional)", lines=3)
        create_btn = gr.Button("Create", variant="primary")
        create_status = gr.Textbox(label="Status", interactive=False)

        refresh_btn.click(
            refresh_chatbots,
            inputs=[token_input],
            outputs=[chatbot_table, gr.Dropdown(), chatbot_choices_state],
        )
        create_btn.click(
            create_chatbot_handler,
            inputs=[token_input, new_name, new_prompt],
            outputs=[create_status],
        )

    with gr.Tab("Documents"):
        with gr.Row():
            chatbot_select_docs = gr.Dropdown(label="Select Chatbot", choices=[])
            refresh_docs_btn = gr.Button("Refresh Documents")
        docs_table = gr.Dataframe(headers=["ID", "Filename", "Status", "Error"])
        gr.Markdown("### Upload Document")
        file_upload = gr.File(
            label="Upload File",
            file_types=[".pdf", ".docx", ".txt", ".html"],
        )
        upload_btn = gr.Button("Upload", variant="primary")
        upload_status = gr.Textbox(label="Status", interactive=False)

        chatbot_select_docs.change(
            lambda token, name, choices: choices.get(name, ""),
            inputs=[token_input, chatbot_select_docs, chatbot_choices_state],
            outputs=[chatbot_id_state],
        )
        refresh_docs_btn.click(
            refresh_documents,
            inputs=[token_input, chatbot_id_state],
            outputs=[docs_table],
        )
        upload_btn.click(
            upload_handler,
            inputs=[token_input, chatbot_id_state, file_upload],
            outputs=[upload_status],
        )

    with gr.Tab("Chat"):
        chatbot_select_chat = gr.Dropdown(label="Select Chatbot", choices=[])
        chat_interface = gr.Chatbot(label="Conversation", height=450)
        with gr.Row():
            msg_input = gr.Textbox(
                label="Message",
                placeholder="Ask a question...",
                scale=4,
            )
            send_btn = gr.Button("Send", variant="primary", scale=1)

        send_btn.click(
            chat_handler,
            inputs=[msg_input, chat_interface, token_input, chatbot_id_state, session_id_state],
            outputs=[chat_interface, session_id_state],
        ).then(lambda: "", outputs=[msg_input])

        msg_input.submit(
            chat_handler,
            inputs=[msg_input, chat_interface, token_input, chatbot_id_state, session_id_state],
            outputs=[chat_interface, session_id_state],
        ).then(lambda: "", outputs=[msg_input])

if __name__ == "__main__":
    demo.launch(
        server_name="0.0.0.0",
        server_port=7860,
        show_api=False,
    )
```

## Implementation notes

- **Dev auth bypass:** In local development, enter `devtest:<clerk_user_id>` as the token. Get a clerk_user_id from the DB: `docker compose exec postgres psql -U postgres -c "SELECT clerk_user_id FROM tenants LIMIT 1;"`.
- **Session ID:** A random UUID is generated per chat tab session. It's stored in `gr.State` so the conversation persists across messages within a single session.
- **Streaming:** Gradio streaming generators must yield cumulative text (not incremental deltas). The `chat_stream` method yields the growing `buffer` string on each token.
- **Sync vs async:** Gradio 5 handlers can be async, but the streaming `chat_stream` must remain synchronous because it wraps `httpx.Client.stream`. The `run()` helper bridges sync Gradio handlers to async API calls for non-streaming calls.
- **Tab synchronization:** The `chatbot_id_state` is updated when the user selects a chatbot in either Documents or Chat tabs. The `chatbot_choices_state` maps chatbot names to IDs (populated on Chatbots tab refresh).
- **Status polling:** Documents start as `pending` and move to `ready` after the worker processes them. The user must manually click "Refresh Documents" to see updated status. A future improvement would add auto-polling.

## Acceptance criteria

- `docker compose up ui` → Gradio app accessible at `http://localhost:7860`.
- Enter `devtest:<id>`, click Refresh on Chatbots tab → list of chatbots appears.
- Create a chatbot → appears in list on next refresh.
- Select chatbot, upload a PDF → status shows `pending`.
- Refresh documents after worker finishes → status shows `ready`.
- Switch to Chat tab, select chatbot, ask a question from the uploaded doc → streaming answer appears in the chat window.
- Off-topic question → fallback "I don't have information..." response.
- Gradio UI works in Chrome, Firefox, Safari.
