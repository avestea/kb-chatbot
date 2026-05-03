# Slice 14 — Thumbs Up / Down Feedback

**Depends on:** Slice 8 (chat endpoint), Slice 9 (Gradio UI), Slice 11 (evaluation dashboard)
**Prereq:** Read `00-prompt-prefix.md` first.

---

## Goal

Let users rate individual chatbot answers with a thumbs up or thumbs down. The feedback lands in a `feedback` table and surfaces in the evaluation dashboard alongside the existing no-answer rate.

No annotation pipeline, no LLM-as-judge — one click, one row in the DB.

---

## Schema change

New table: `feedback`

```sql
CREATE TABLE feedback (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    message_id  uuid NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    chatbot_id  uuid NOT NULL REFERENCES chatbots(id) ON DELETE CASCADE,
    tenant_id   uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    rating      smallint NOT NULL CHECK (rating IN (-1, 1)),  -- -1 = down, 1 = up
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX feedback_message_uq ON feedback(message_id);
CREATE INDEX feedback_chatbot_idx ON feedback(chatbot_id);
```

One row per message — a second vote on the same message upserts the rating.

Generate the migration:
```bash
docker compose exec api alembic revision --autogenerate -m "add_feedback"
# Review the generated file; the table won't autogenerate (not in models.py) — hand-write it:
docker compose exec api alembic upgrade head
```

### ORM model (`api/src/db/models.py`)

```python
class Feedback(Base):
    __tablename__ = "feedback"
    __table_args__ = (
        Index("feedback_chatbot_idx", "chatbot_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("messages.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    chatbot_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chatbots.id", ondelete="CASCADE"), nullable=False
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    rating: Mapped[int] = mapped_column(Integer, nullable=False)   # -1 or 1
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
```

---

## Backend — feedback route (`api/src/routes/feedback.py`)

```python
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy import select
from uuid import UUID

from src.db.base import get_db
from src.db.models import Feedback, Message, Conversation, Chatbot
from src.db.tenant_scope import tenant_where
from src.auth.authenticate import get_current_tenant, AuthenticatedTenant
from src.lib.errors import NotFoundError, BadRequestError

router = APIRouter(prefix="/feedback", tags=["feedback"])


class SubmitFeedbackRequest(BaseModel):
    message_id: UUID
    rating: int = Field(..., ge=-1, le=1)   # -1 or 1; 0 is rejected

    def model_post_init(self, __context):
        if self.rating == 0:
            raise ValueError("rating must be -1 or 1")


@router.post("", status_code=201)
async def submit_feedback(
    body: SubmitFeedbackRequest,
    auth: AuthenticatedTenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
):
    # Verify the message belongs to this tenant
    bot_ids = [r for (r,) in (await db.execute(
        select(Chatbot.id).where(tenant_where(Chatbot, auth.tenant_id))
    )).all()]

    message = (await db.execute(
        select(Message)
        .join(Conversation, Message.conversation_id == Conversation.id)
        .where(
            Message.id == body.message_id,
            Message.role == "assistant",
            Conversation.chatbot_id.in_(bot_ids),
        )
    )).scalar_one_or_none()

    if message is None:
        raise NotFoundError("Message not found")

    conversation = (await db.execute(
        select(Conversation).where(Conversation.id == message.conversation_id)
    )).scalar_one()

    stmt = pg_insert(Feedback).values(
        message_id=body.message_id,
        chatbot_id=conversation.chatbot_id,
        tenant_id=auth.tenant_id,
        rating=body.rating,
    ).on_conflict_do_update(
        index_elements=["message_id"],
        set_={"rating": body.rating},
    )
    await db.execute(stmt)
    await db.commit()

    return {"message_id": str(body.message_id), "rating": body.rating}
```

Register in `main.py`:
```python
from src.routes.feedback import router as feedback_router
api_v1.include_router(feedback_router)
```

---

## Chat endpoint: capture message_id (`api/src/routes/chat.py`)

The `done` SSE event already exists. Ensure it carries `message_id` so the Gradio UI can bind the feedback buttons to the right message:

```python
yield sse("done", {"message_id": str(assistant_msg.id)})
```

---

## API client (`web/api_client.py`)

```python
async def submit_feedback(self, message_id: str, rating: int) -> None:
    async with self._client() as c:
        r = await c.post("/api/v1/feedback", json={"message_id": message_id, "rating": rating})
        r.raise_for_status()
```

In `chat_stream`, capture `message_id` from the `done` event via an `on_done` callback (similar to `on_sources`):

```python
def chat_stream(
    self,
    chatbot_id: str,
    message: str,
    session_id: str,
    on_sources=None,
    on_done=None,     # callable(message_id: str)
) -> Iterator[str]:
    ...
    elif current_event == "done":
        if on_done and "message_id" in data:
            on_done(data["message_id"])
```

---

## Gradio UI changes (`web/app.py`)

### State for last message_id

```python
last_message_id_state = gr.State("")
```

### Feedback buttons in the Chat tab

```python
with gr.Tab("Chat"):
    ...
    sources_display = gr.Dataframe(...)

    with gr.Row(visible=False) as feedback_row:
        thumbs_up_btn   = gr.Button("👍", variant="secondary", scale=1)
        thumbs_down_btn = gr.Button("👎", variant="secondary", scale=1)
        feedback_status = gr.Markdown("")
```

### Updated chat handler

```python
def chat_handler(message, history, token, chatbot_id, session_id_state):
    ...
    message_id_captured = []

    def capture_sources(sources):
        sources_captured.extend(sources)

    def capture_done(message_id):
        message_id_captured.append(message_id)

    for chunk in get_client(token).chat_stream(
        chatbot_id, message, session_id,
        on_sources=capture_sources,
        on_done=capture_done,
    ):
        ...
        yield history + [[message, partial]], session_id, gr.update(visible=False), "", gr.update(visible=False)

    message_id = message_id_captured[0] if message_id_captured else ""
    yield (
        history + [[message, partial]],
        session_id,
        gr.update(value=rows, visible=bool(rows)),   # sources
        message_id,                                   # last_message_id_state
        gr.update(visible=bool(message_id)),          # feedback_row
    )

send_btn.click(
    chat_handler,
    inputs=[msg_input, chat_display, token_input, chatbot_id_state, session_id_state],
    outputs=[chat_display, session_id_state, sources_display, last_message_id_state, feedback_row],
).then(lambda: "", outputs=[msg_input])
```

### Feedback button handlers

```python
def send_feedback(token, message_id, rating):
    if not message_id:
        return "No message to rate."
    run(get_client(token).submit_feedback(message_id, rating))
    return "Thanks for your feedback!" if rating == 1 else "Got it — we'll improve."

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
```

---

## Evaluation dashboard additions (`api/src/routes/analytics.py`)

Add feedback counts to the summary endpoint:

```python
from src.db.models import Feedback

# Inside analytics_summary(), after existing stats:

feedback_stats = (await db.execute(
    select(
        func.count(Feedback.id).label("total"),
        func.sum(func.cast(Feedback.rating == 1, Integer)).label("thumbs_up"),
        func.sum(func.cast(Feedback.rating == -1, Integer)).label("thumbs_down"),
    )
    .join(Chatbot, Feedback.chatbot_id == Chatbot.id)
    .where(Chatbot.id.in_(bot_ids))
)).mappings().one()

total_feedback = int(feedback_stats["total"] or 0)
thumbs_up = int(feedback_stats["thumbs_up"] or 0)
thumbs_down = int(feedback_stats["thumbs_down"] or 0)
satisfaction_rate = round(thumbs_up / total_feedback, 3) if total_feedback else None

return {
    ...existing fields...,
    "total_feedback": total_feedback,
    "thumbs_up": thumbs_up,
    "thumbs_down": thumbs_down,
    "satisfaction_rate": satisfaction_rate,
}
```

Add two more stat cards to the Gradio Evaluation tab:

```python
with gr.Row():
    stat_total_convs  = gr.Number(label="Total conversations",   interactive=False)
    stat_total_msgs   = gr.Number(label="Total messages",        interactive=False)
    stat_no_answer    = gr.Number(label="No-answer rate (%)",    interactive=False)
    stat_avg_sim      = gr.Number(label="Avg top similarity",    interactive=False)
    stat_satisfaction = gr.Number(label="Satisfaction rate (%)", interactive=False)
```

Also expose rating per message in the conversation inspector — the `messages` endpoint already returns the full message; add a feedback JOIN:

```python
# In get_conversation_messages(), extend the message query:
from sqlalchemy.orm import outerjoin

rows = (await db.execute(
    select(Message, Feedback.rating)
    .outerjoin(Feedback, Feedback.message_id == Message.id)
    .where(Message.conversation_id == conversation_id)
    .order_by(Message.created_at)
)).all()

return {
    "messages": [
        {
            ...existing fields...,
            "rating": rating,   # 1, -1, or None
        }
        for message, rating in rows
    ]
}
```

---

## Acceptance criteria

- `POST /api/v1/feedback` with `rating=1` or `rating=-1` for a valid `message_id` → 201.
- Submitting feedback twice for the same message upserts the rating (no duplicate rows).
- Feedback for a message belonging to another tenant → 404.
- Thumbs up/down buttons appear in the Gradio Chat tab after each assistant response.
- Clicking a button sends feedback and shows a confirmation message.
- Evaluation dashboard summary includes `thumbs_up`, `thumbs_down`, and `satisfaction_rate`.
- Conversation inspector shows `rating` (1, -1, or null) on each assistant message.
