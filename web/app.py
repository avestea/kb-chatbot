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

def _bots_to_markdown(bots):
    if not bots:
        return "*No chatbots yet.*"
    lines = ["| ID | Name | Created |", "|---|---|---|"]
    for b in bots:
        lines.append(f"| `{b['id'][:42]}` | {b['name']} | {b['created_at'][:19]} |")
    return "\n".join(lines)

def refresh_chatbots(token):
    bots = run(get_client(token).list_chatbots())
    choices = {b["name"]: b["id"] for b in bots}
    names = list(choices.keys())
    return (
        gr.update(value=_bots_to_markdown(bots)),
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

def _docs_to_markdown(docs):
    if not docs:
        return "*No documents.*"
    lines = ["| ID | Filename | Status | Error |", "|---|---|---|---|"]
    for d in docs:
        err = (d.get("error_reason") or "").replace("|", "\\|")
        lines.append(f"| `{d['id'][:42]}` | {d['filename']} | {d['status']} | {err} |")
    return "\n".join(lines)

def refresh_documents(token, chatbot_id):
    if not chatbot_id:
        return gr.update(value="")
    docs = run(get_client(token).list_documents(chatbot_id))
    return gr.update(value=_docs_to_markdown(docs))

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

    choices = []
    for c in convs:
        label = (
            f"{'❌' if c.get('has_failure') else '✓'}  "
            f"{c['id'][:8]}…  |  "
            f"{(c.get('first_question') or '')[:60]}  |  "
            f"{c['created_at'][:19]}"
        )
        choices.append((label, c["id"]))

    no_answer_pct = round(summary.get("no_answer_rate", 0) * 100, 1)
    avg_sim = summary.get("avg_top_similarity")
    satisfaction = summary.get("satisfaction_rate")
    satisfaction_pct = round(satisfaction * 100, 1) if satisfaction is not None else 0

    return (
        summary.get("total_conversations", 0),
        summary.get("total_messages", 0),
        no_answer_pct,
        avg_sim if avg_sim is not None else 0,
        satisfaction_pct,
        gr.update(choices=choices, value=None),
    )


def inspect_conversation(token, conv_id):
    if not conv_id or not conv_id.strip():
        raise gr.Error("Select or enter a conversation ID first.")
    messages = run(get_client(token).get_conversation_messages(conv_id.strip()))
    return messages


# ─── Tab: Chat ───────────────────────────────────────────────────────────────

def _sources_to_markdown(sources):
    if not sources:
        return ""
    lines = [
        "**Sources used**",
        "",
        "| # | Document | Match | Similarity | Snippet |",
        "|---|----------|-------|------------|---------|",
    ]
    for s in sources:
        doc = str(s["document_name"]).replace("|", "\\|")
        match = s.get("match_type", "semantic")
        sim = f"{s['similarity']:.3f}"
        snippet = s["snippet"][:120].replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {s['index']} | {doc} | {match} | {sim} | {snippet} |")
    return "\n".join(lines)


def send_feedback(token, message_id, rating):
    if not message_id:
        return "No message to rate."
    run(get_client(token).submit_feedback(message_id, rating))
    return "Thanks for your feedback!" if rating == 1 else "Got it — we'll improve."


async def chat_handler(message, history, token, chatbot_id, session_id_state):
    if not chatbot_id:
        yield (
            history + [
                {"role": "user", "content": message},
                {"role": "assistant", "content": "Select a chatbot first."},
            ],
            session_id_state,
            gr.skip(),
            gr.skip(),
            gr.skip(),
            "",
            gr.update(visible=False),
            "",
        )
        return
    session_id = session_id_state or str(uuid.uuid4())
    accumulated = history + [{"role": "user", "content": message}]

    sources_captured = []
    retrieval_query_captured = []
    message_id_captured = []

    def capture_sources(sources):
        sources_captured.extend(sources)

    def capture_meta(meta):
        rq = meta.get("retrieval_query", "")
        if rq:
            retrieval_query_captured.append(rq)

    def capture_done(message_id):
        message_id_captured.append(message_id)

    import time as _time
    t0 = _time.monotonic()
    print(f"[chat] start ts={t0:.3f}", flush=True)
    partial = ""
    chunk_count = 0
    async for chunk in get_client(token).chat_stream(
        chatbot_id, message, session_id,
        on_sources=capture_sources,
        on_meta=capture_meta,
        on_done=capture_done,
    ):
        partial = chunk
        chunk_count += 1
        yield (
            accumulated + [{"role": "assistant", "content": partial}],
            session_id,
            gr.skip(),
            gr.skip(),
            gr.skip(),
            "",
            gr.update(visible=False),
            "",
        )
    t1 = _time.monotonic()
    print(f"[chat] stream done after {t1 - t0:.3f}s, chunks={chunk_count}", flush=True)

    sources_md = _sources_to_markdown(sources_captured)
    rq = retrieval_query_captured[0] if retrieval_query_captured else ""
    message_id = message_id_captured[0] if message_id_captured else ""
    yield (
        accumulated + [{"role": "assistant", "content": partial or "..."}],
        session_id,
        gr.update(value=sources_md),
        message_id,
        gr.update(value=f"*Searched for: {rq}*" if rq else "", visible=bool(rq)),
        gr.skip(),
        gr.update(visible=True),
        "",
    )
    t2 = _time.monotonic()
    print(f"[chat] handler returning total={t2 - t0:.3f}s", flush=True)

# ─── Layout ──────────────────────────────────────────────────────────────────

with gr.Blocks(title="KB Chatbot") as demo:
    gr.Markdown("# Knowledge Base Chatbot")
    token_input = gr.Textbox(label="API Token", type="password", placeholder="devtest:your-clerk-id")
    chatbot_id_state = gr.State("")
    session_id_state = gr.State("")
    last_message_id_state = gr.State("")
    chatbot_choices_state = gr.State({})

    with gr.Tab("Chatbots"):
        with gr.Row():
            refresh_btn = gr.Button("Refresh", variant="secondary")
        chatbot_table = gr.Markdown(value="")
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
        docs_table = gr.Markdown(value="")
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
        with gr.Row(visible=False) as feedback_row:
            thumbs_up_btn = gr.Button("👍", variant="secondary", scale=1)
            thumbs_down_btn = gr.Button("👎", variant="secondary", scale=1)
            feedback_status = gr.Markdown("")
        with gr.Row():
            msg_input = gr.Textbox(
                label="Message",
                placeholder="Ask a question...",
                scale=4,
            )
            send_btn = gr.Button("Send", variant="primary", scale=1)
        sources_display = gr.Markdown(value="")
        retrieval_query_display = gr.Markdown(value="", visible=False)

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
            stat_satisfaction = gr.Number(label="Satisfaction rate (%)", interactive=False)

        gr.Markdown("### Conversations")
        show_failures_only = gr.Checkbox(label="Show failures only (no-answer responses) — then press Refresh", value=False)
        conv_table = gr.Dropdown(label="Select conversation", choices=[], interactive=True)

        gr.Markdown("### Conversation detail")
        conv_id_input = gr.Textbox(label="Conversation ID", placeholder="select above or paste")
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

    _chat_outputs = [
        chat_interface,
        session_id_state,
        sources_display,
        last_message_id_state,
        retrieval_query_display,
        msg_input,
        feedback_row,
        feedback_status,
    ]

    send_btn.click(
        chat_handler,
        inputs=[msg_input, chat_interface, token_input, chatbot_id_state, session_id_state],
        outputs=_chat_outputs,
    )

    msg_input.submit(
        chat_handler,
        inputs=[msg_input, chat_interface, token_input, chatbot_id_state, session_id_state],
        outputs=_chat_outputs,
    )

    thumbs_up_btn.click(
        lambda token, mid: send_feedback(token, mid, 1),
        inputs=[token_input, last_message_id_state],
        outputs=[feedback_status],
    )
    thumbs_down_btn.click(
        lambda token, mid: send_feedback(token, mid, -1),
        inputs=[token_input, last_message_id_state],
        outputs=[feedback_status],
    )

    refresh_eval_btn.click(
        refresh_eval,
        inputs=[token_input, eval_chatbot_select, chatbot_choices_state, show_failures_only],
        outputs=[stat_total_convs, stat_total_msgs, stat_no_answer, stat_avg_sim, stat_satisfaction, conv_table],
    )

    conv_table.change(
        lambda val: val if val else "",
        inputs=[conv_table],
        outputs=[conv_id_input],
        queue=False,
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
