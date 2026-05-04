"""add_feedback

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-04

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


revision: str = '0004'
down_revision: Union[str, None] = '0003'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE feedback (
            id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            message_id  uuid NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
            chatbot_id  uuid NOT NULL REFERENCES chatbots(id) ON DELETE CASCADE,
            tenant_id   uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            rating      smallint NOT NULL CHECK (rating IN (-1, 1)),
            created_at  timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE UNIQUE INDEX feedback_message_uq ON feedback(message_id)")
    op.execute("CREATE INDEX feedback_chatbot_idx ON feedback(chatbot_id)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS feedback")
