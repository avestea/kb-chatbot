# Slice 11 — Evaluation Dashboard

**Depends on:** Slice 9 (Gradio UI), Slice 10 (source_chunks on messages)
**Prereq:** Read `00-prompt-prefix.md` first.

---

## Goal

A new "Evaluation" tab in the Gradio UI that lets you see where the chatbot is working and where it is failing — using data already in the database. No LLM-as-judge. No extra API calls per answer.

**What it shows:**
- Summary stats: total conversations, total messages, no-answer rate
- Failure browser: conversations where the bot had no relevant context
- Conversation inspector: pick any conversation and see the full exchange with sources and similarity scores per turn

---

## Backend — new analytics routes (`api/src/routes/analytics.py`)

```python
from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, func, cast, Float
from sqlalchemy.ext.asyncio import AsyncSession
from uuid import UUID

from src.db.base import get_db
from src.db.models import Chatbot, Conversation, Message
from src.db.tenant_scope import tenant_where
from src.auth.authenticate import get_current_tenant, AuthenticatedTenant

router = APIRouter(prefix="/analytics", tags=["analytics"])


@router.get("/summary")
async def analytics_summary(
    chatbot_id: UUID | None = Query(default=None),
    auth: AuthenticatedTenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns aggregate stats across all chatbots (or a single chatbot).
    Scoped to the authenticated tenant.
    """
    # Build chatbot id filter
    bot_ids_q = select(Chatbot.id).where(tenant_where(Chatbot, auth.tenant_id))
    if chatbot_id:
        bot_ids_q = bot_ids_q.where(Chatbot.id == chatbot_id)
    bot_ids = [r for (r,) in (await db.execute(bot_ids_q)).all()]

    if not bot_ids:
        return {"total_conversations": 0, "total_messages": 0,
                "no_answer_rate": 0.0, "avg_similarity": None}

    # Conversation count
    total_convs = (await db.execute(
        select(func.count(Conversation.id))
        .where(Conversation.chatbot_id.in_(bot_ids))
    )).scalar_one()

    # Message stats — only assistant messages
    msg_stats = (await db.execute(
        select(
            func.count(Message.id).label("total"),
            func.sum(func.cast(Message.no_answer, Float)).label("no_answer_count"),
        )
        .join(Conversation, Message.conversation_id == Conversation.id)
        .where(
            Conversation.chatbot_id.in_(bot_ids),
            Message.role == "assistant",
        )
    )).mappings().one()

    total_msgs = int(msg_stats["total"] or 0)
    no_answer_count = int(msg_stats["no_answer_count"] or 0)
    no_answer_rate = round(no_answer_count / total_msgs, 3) if total_msgs else 0.0

    # Average similarity — computed from source_chunks JSONB
    # Extract the max similarity per message, then average across messages
    avg_sim_result = (await db.execute(
        select(
            func.avg(
                func.cast(
                    func.jsonb_path_query_first(
                        Message.source_chunks,
                        cast("$[0].similarity", type_=None),  # first (highest) chunk similarity
                    ),
                    Float,
                )
            )
        )
        .join(Conversation, Message.conversation_id == Conversation.id)
        .where(
            Conversation.chatbot_id.in_(bot_ids),
            Message.role == "assistant",
            Message.source_chunks.isnot(None),
            Message.no_answer == False,
        )
    )).scalar_one()

    return {
        "total_conversations": total_convs,
        "total_messages": total_msgs,
        "no_answer_count": no_answer_count,
        "no_answer_rate": no_answer_rate,
        "avg_top_similarity": round(float(avg_sim_result), 3) if avg_sim_result else None,
    }


@router.get("/conversations")
async def list_conversations(
    chatbot_id: UUID | None = Query(default=None),
    no_answer_only: bool = Query(default=False),
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    auth: AuthenticatedTenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    """
    Lists conversations with a preview of the first user message
    and whether any assistant turn had no_answer=True.
    """
    bot_ids_q = select(Chatbot.id).where(tenant_where(Chatbot, auth.tenant_id))
    if chatbot_id:
        bot_ids_q = bot_ids_q.where(Chatbot.id == chatbot_id)
    bot_ids = [r for (r,) in (await db.execute(bot_ids_q)).all()]

    if not bot_ids:
        return {"items": [], "total": 0, "has_more": False}

    # Subquery: does any message in the conversation have no_answer=True?
    has_failure = (
        select(func.bool_or(Message.no_answer))
        .where(
            Message.conversation_id == Conversation.id,
            Message.role == "assistant",
        )
        .scalar_subquery()
    )

    # First user message as preview
    first_question = (
        select(Message.content)
        .where(
            Message.conversation_id == Conversation.id,
            Message.role == "user",
        )
        .order_by(Message.created_at)
        .limit(1)
        .scalar_subquery()
    )

    query = (
        select(
            Conversation.id,
            Conversation.chatbot_id,
            Conversation.created_at,
            first_question.label("first_question"),
            has_failure.label("has_failure"),
        )
        .where(Conversation.chatbot_id.in_(bot_ids))
    )

    if no_answer_only:
        query = query.where(has_failure)

    total = (await db.execute(
        select(func.count()).select_from(query.subquery())
    )).scalar_one()

    rows = (await db.execute(
        query.order_by(Conversation.created_at.desc()).limit(limit).offset(offset)
    )).mappings().all()

    return {
        "items": [dict(r) for r in rows],
        "total": total,
        "has_more": total > offset + len(rows),
    }


@router.get("/conversations/{conversation_id}/messages")
async def get_conversation_messages(
    conversation_id: UUID,
    auth: AuthenticatedTenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    """Full message thread for a conversation, with source chunks on each assistant turn."""
    # Verify conversation belongs to this tenant via chatbot
    bot_ids = [r for (r,) in (await db.execute(
        select(Chatbot.id).where(tenant_where(Chatbot, auth.tenant_id))
    )).all()]

    conv = (await db.execute(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.chatbot_id.in_(bot_ids),
        )
    )).scalar_one_or_none()

    if conv is None:
        from src.lib.errors import NotFoundError
        raise NotFoundError()

    messages = (await db.execute(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at)
    )).scalars().all()

    return {
        "conversation_id": str(conversation_id),
        "messages": [
            {
                "id": str(m.id),
                "role": m.role,
                "content": m.content,
                "no_answer": m.no_answer,
                "tokens_used": m.tokens_used,
                "source_chunks": m.source_chunks or [],
                "created_at": m.created_at.isoformat(),
            }
            for m in messages
        ],
    }
```

Register in `main.py`:
```python
from src.routes.analytics import router as analytics_router
api_v1.include_router(analytics_router)
```

---

## API client additions (`web/api_client.py`)

```python
async def get_analytics_summary(self, chatbot_id: str | None = None) -> dict:
    params = {}
    if chatbot_id:
        params["chatbot_id"] = chatbot_id
    async with self._client() as c:
        r = await c.get("/api/v1/analytics/summary", params=params)
        r.raise_for_status()
        return r.json()

async def list_conversations(
    self,
    chatbot_id: str | None = None,
    no_answer_only: bool = False,
    limit: int = 25,
) -> list[dict]:
    params = {"limit": limit, "no_answer_only": str(no_answer_only).lower()}
    if chatbot_id:
        params["chatbot_id"] = chatbot_id
    async with self._client() as c:
        r = await c.get("/api/v1/analytics/conversations", params=params)
        r.raise_for_status()
        return r.json()["items"]

async def get_conversation_messages(self, conversation_id: str) -> list[dict]:
    async with self._client() as c:
        r = await c.get(f"/api/v1/analytics/conversations/{conversation_id}/messages")
        r.raise_for_status()
        return r.json()["messages"]
```

---

## Gradio — Evaluation tab (`web/app.py`)

```python
with gr.Tab("Evaluation"):
    gr.Markdown("## Chatbot Quality Dashboard")

    with gr.Row():
        eval_chatbot_select = gr.Dropdown(label="Filter by chatbot (optional)", choices=["All"])
        refresh_eval_btn = gr.Button("Refresh", variant="secondary")

    # ── Summary cards ────────────────────────────────────────��─────────
    with gr.Row():
        stat_total_convs  = gr.Number(label="Total conversations", interactive=False)
        stat_total_msgs   = gr.Number(label="Total messages",       interactive=False)
        stat_no_answer    = gr.Number(label="No-answer rate (%)",   interactive=False)
        stat_avg_sim      = gr.Number(label="Avg top similarity",   interactive=False)

    # ── Conversation browser ───────────────────────────────────────────
    gr.Markdown("### Conversations")
    show_failures_only = gr.Checkbox(label="Show failures only (no-answer responses)", value=False)
    conv_table = gr.Dataframe(
        headers=["ID", "Chatbot", "First question", "Has failure", "Started"],
        interactive=False,
    )
    selected_conv_id = gr.State("")

    # ── Message inspector ──────────────────────────────────────────────
    gr.Markdown("### Conversation detail")
    gr.Markdown("*Click a row above then press Inspect.*")
    conv_id_input = gr.Textbox(label="Conversation ID", placeholder="paste or select above")
    inspect_btn = gr.Button("Inspect", variant="secondary")
    message_detail = gr.JSON(label="Messages + sources")


def refresh_eval(token, chatbot_name, chatbot_choices, failures_only):
    client = get_client(token)
    chatbot_id = chatbot_choices.get(chatbot_name) if chatbot_name != "All" else None

    summary = run(client.get_analytics_summary(chatbot_id))
    convs   = run(client.list_conversations(chatbot_id, no_answer_only=failures_only))

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
        gr.Dataframe(value=rows),
    )


def inspect_conversation(token, conv_id):
    if not conv_id.strip():
        raise gr.Error("Enter a conversation ID first.")
    messages = run(get_client(token).get_conversation_messages(conv_id.strip()))
    return messages


refresh_eval_btn.click(
    refresh_eval,
    inputs=[token_input, eval_chatbot_select, chatbot_choices_state, show_failures_only],
    outputs=[stat_total_convs, stat_total_msgs, stat_no_answer, stat_avg_sim, conv_table],
)
show_failures_only.change(
    refresh_eval,
    inputs=[token_input, eval_chatbot_select, chatbot_choices_state, show_failures_only],
    outputs=[stat_total_convs, stat_total_msgs, stat_no_answer, stat_avg_sim, conv_table],
)
inspect_btn.click(
    inspect_conversation,
    inputs=[token_input, conv_id_input],
    outputs=[message_detail],
)
```

---

## What the dashboard reveals

| Metric | What it means | What to do |
|---|---|---|
| **No-answer rate > 20%** | Many questions have no relevant context | Upload more documents; broaden coverage |
| **Avg similarity < 0.78** | Retrieved chunks are weakly related | Improve chunking strategy (smaller chunks, more overlap) or re-embed |
| **Specific questions always failing** | Gap in the knowledge base | Identify the topic and add a document covering it |
| **High similarity but wrong answer** | Retrieval is fine; LLM is misinterpreting | Tune `system_prompt_override` on the chatbot |

---

## Acceptance criteria

- `GET /api/v1/analytics/summary` returns correct counts after seeding test conversations.
- `no_answer_rate` reflects actual `no_answer=True` flags in the messages table.
- `GET /api/v1/analytics/conversations?no_answer_only=true` returns only conversations with at least one failed answer.
- Evaluation tab in Gradio loads summary stats and conversation list on Refresh.
- "Show failures only" checkbox filters the table immediately.
- Inspecting a conversation ID shows the full message thread with `source_chunks` per assistant turn.
- A tenant cannot inspect conversations belonging to another tenant (404).
