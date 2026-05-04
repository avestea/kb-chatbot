"""add_chunks_tsvector

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-04

"""
from typing import Sequence, Union
from alembic import op


revision: str = '0003'
down_revision: Union[str, None] = '0002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE chunks
          ADD COLUMN content_tsv tsvector
          GENERATED ALWAYS AS (to_tsvector('english', content)) STORED
    """)
    op.execute("CREATE INDEX chunks_content_tsv_idx ON chunks USING gin(content_tsv)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS chunks_content_tsv_idx")
    op.drop_column('chunks', 'content_tsv')
