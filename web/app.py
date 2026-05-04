import gradio as gr
import asyncio
import threading
import os
import uuid
from api_client import APIClient

# ─── Helpers ────────────────────────────────────────────────────────────────

# Single background event loop shared across all Gradio handlers.
# Avoids spawning a new thread + loop for every async call.
_bg_loop = asyncio.new_event_loop()
threading.Thread(target=_bg_loop.run_forever, daemon=True).start()

def run(coro):
    """Submit a coroutine to the background loop and block until done."""
    return asyncio.run_coroutine_threadsafe(coro, _bg_loop).result()

def run_parallel(*coros):
    """Run multiple coroutines concurrently and return all results."""
    async def _gather():
        return await asyncio.gather(*coros)
    return asyncio.run_coroutine_threadsafe(_gather(), _bg_loop).result()

_client_cache: dict[str, APIClient] = {}

def get_client(token: str) -> APIClient:
    token = token.strip()
    if not token:
        raise gr.Error("Enter your API token first.")
    if token not in _client_cache:
        _client_cache[token] = APIClient(token)
    return _client_cache[token]

# ─── Tab: Chatbots ───────────────────────────────────────────────────────────

def refresh_chatbots(token):
    bots = run(get_client(token).list_chatbots())
    rows = [[b["id"], b["name"], b["created_at"]] for b in bots]
    choices = {b["name"]: b["id"] for b in bots}
    names = list(choices.keys())
    return (
        gr.update(value=rows),
        gr.update(choices=names),
        gr.update(choices=names),
        choices,
        gr.update(choices=["All"] + names),
    )

def create_chatbot_handler(token, name, system_prompt):
    if not name.strip():
        raise gr.Error("Name is required.")
    bot = run(get_client(token).create_chatbot(name.strip(), system_prompt.strip() or None))
    return f"Created: {bot['name']} ({bot['id']})"

# ─── Tab: Documents ──────────────────────────────────────────────────────────

def _doc_rows(token, chatbot_id):
    if not chatbot_id:
        return []
    docs = run(get_client(token).list_documents(chatbot_id))
    return [[d["id"], d["filename"], d["status"], d.get("error_reason", "")] for d in docs]

def refresh_documents(token, chatbot_id):
    return gr.update(value=_doc_rows(token, chatbot_id))

def on_chatbot_select_docs(token, name, choices):
    chatbot_id = choices.get(name, "") if name else ""
    return chatbot_id

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

# ─── Tab: Evaluation ────────────────────────────────────────────────────────

def refresh_eval(token, chatbot_name, chatbot_choices, failures_only):
    client = get_client(token)
    chatbot_id = chatbot_choices.get(chatbot_name) if chatbot_name and chatbot_name != "All" else None

    summary, convs = run_parallel(
        client.get_analytics_summary(chatbot_id),
        client.list_conversations(chatbot_id, no_answer_only=failures_only),
    )

    rows = [
        [
            c["id"][:8] + "…",
            c.get("chatbot_id", "")[:8] + "…",
            (c.get("first_question") or "")[:80],
            "❌" if c.get("has_failure") else "✓",
            c["created_at"][:19],
        ]
        for c in convs
    ]

    no_answer_pct = round(summary.get("no_answer_rate", 0) * 100, 1)
    avg_sim = summary.get("avg_top_similarity")

    return (
        summary.get("total_conversations", 0),
        summary.get("total_messages", 0),
        no_answer_pct,
        avg_sim if avg_sim is not None else 0,
        gr.update(value=rows),
    )


def inspect_conversation(token, conv_id):
    if not conv_id.strip():
        raise gr.Error("Enter a conversation ID first.")
    messages = run(get_client(token).get_conversation_messages(conv_id.strip()))
    return messages


# ─── Tab: Chat ───────────────────────────────────────────────────────────────

def chat_handler(message, history, token, chatbot_id, session_id_state):
    if not chatbot_id:
        yield (
            history + [
                {"role": "user", "content": message},
                {"role": "assistant", "content": "Select a chatbot first."},
            ],
            session_id_state,
            gr.update(visible=False),
        )
        return
    session_id = session_id_state or str(uuid.uuid4())
    accumulated = history + [{"role": "user", "content": message}]

    sources_captured = []

    def capture_sources(sources):
        sources_captured.extend(sources)

    partial = ""
    for chunk in get_client(token).chat_stream(chatbot_id, message, session_id, on_sources=capture_sources):
        partial = chunk
        yield (
            accumulated + [{"role": "assistant", "content": partial}],
            session_id,
            gr.update(visible=False),
        )

    rows = [
        [s["index"], s["document_name"], s["similarity"], s["snippet"]]
        for s in sources_captured
    ]
    yield (
        accumulated + [{"role": "assistant", "content": partial or "..."}],
        session_id,
        gr.update(value=rows, visible=bool(rows)),
    )

# ─── Layout ──────────────────────────────────────────────────────────────────

with gr.Blocks(title="KB Chatbot") as demo:
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
        sources_display = gr.Dataframe(
            headers=["#", "Document", "Similarity", "Snippet"],
            label="Sources used",
            visible=False,
        )

    with gr.Tab("Evaluation"):
        gr.Markdown("## Chatbot Quality Dashboard")

        with gr.Row():
            eval_chatbot_select = gr.Dropdown(label="Filter by chatbot (optional)", choices=["All"])
            refresh_eval_btn = gr.Button("Refresh", variant="secondary")

        with gr.Row():
            stat_total_convs = gr.Number(label="Total conversations", interactive=False)
            stat_total_msgs = gr.Number(label="Total messages", interactive=False)
            stat_no_answer = gr.Number(label="No-answer rate (%)", interactive=False)
            stat_avg_sim = gr.Number(label="Avg top similarity", interactive=False)

        gr.Markdown("### Conversations")
        show_failures_only = gr.Checkbox(label="Show failures only (no-answer responses) — then press Refresh", value=False)
        conv_table = gr.Dataframe(
            headers=["ID", "Chatbot", "First question", "Has failure", "Started"],
            interactive=False,
        )
        selected_conv_id = gr.State("")

        gr.Markdown("### Conversation detail")
        gr.Markdown("*Click a row above then press Inspect.*")
        conv_id_input = gr.Textbox(label="Conversation ID", placeholder="paste or select above")
        inspect_btn = gr.Button("Inspect", variant="secondary")
        message_detail = gr.JSON(label="Messages + sources")

    # ─── Event wiring ────────────────────────────────────────────────────────

    refresh_btn.click(
        refresh_chatbots,
        inputs=[token_input],
        outputs=[chatbot_table, chatbot_select_docs, chatbot_select_chat, chatbot_choices_state, eval_chatbot_select],
    )
    create_btn.click(
        create_chatbot_handler,
        inputs=[token_input, new_name, new_prompt],
        outputs=[create_status],
    ).then(
        refresh_chatbots,
        inputs=[token_input],
        outputs=[chatbot_table, chatbot_select_docs, chatbot_select_chat, chatbot_choices_state, eval_chatbot_select],
    )

    chatbot_select_docs.change(
        on_chatbot_select_docs,
        inputs=[token_input, chatbot_select_docs, chatbot_choices_state],
        outputs=[chatbot_id_state],
        queue=False,
    )
    chatbot_select_chat.change(
        lambda token, name, choices: choices.get(name, ""),
        inputs=[token_input, chatbot_select_chat, chatbot_choices_state],
        outputs=[chatbot_id_state],
        queue=False,
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

    send_btn.click(
        chat_handler,
        inputs=[msg_input, chat_interface, token_input, chatbot_id_state, session_id_state],
        outputs=[chat_interface, session_id_state, sources_display],
    ).then(lambda: "", outputs=[msg_input])

    msg_input.submit(
        chat_handler,
        inputs=[msg_input, chat_interface, token_input, chatbot_id_state, session_id_state],
        outputs=[chat_interface, session_id_state, sources_display],
    ).then(lambda: "", outputs=[msg_input])

    refresh_eval_btn.click(
        refresh_eval,
        inputs=[token_input, eval_chatbot_select, chatbot_choices_state, show_failures_only],
        outputs=[stat_total_convs, stat_total_msgs, stat_no_answer, stat_avg_sim, conv_table],
    )
    inspect_btn.click(
        inspect_conversation,
        inputs=[token_input, conv_id_input],
        outputs=[message_detail],
    )

if __name__ == "__main__":
    demo.queue(default_concurrency_limit=10)
    demo.launch(
        server_name="0.0.0.0",
        server_port=7860,
        theme=gr.themes.Soft(),
    )
