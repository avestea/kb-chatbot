"""init

Revision ID: 0001
Revises:
Create Date: 2026-05-03

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from pgvector.sqlalchemy import Vector


# revision identifiers, used by Alembic.
revision: str = '0001'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'tenants',
        sa.Column('id', sa.UUID(as_uuid=True), nullable=False, primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column('name', sa.Text(), nullable=False),
        sa.Column('plan', sa.String(length=20), nullable=False, server_default='free'),
        sa.Column('clerk_user_id', sa.Text(), nullable=False),
        sa.Column('stripe_customer_id', sa.Text()),
        sa.Column('stripe_subscription_id', sa.Text()),
        sa.Column('deleted_at', sa.DateTime(timezone=True)),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    )

    op.create_table(
        'chatbots',
        sa.Column('id', sa.UUID(as_uuid=True), nullable=False, primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column('tenant_id', sa.UUID(as_uuid=True), nullable=False),
        sa.Column('name', sa.Text(), nullable=False),
        sa.Column('system_prompt_override', sa.Text()),
        sa.Column('deleted_at', sa.DateTime(timezone=True)),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    )

    op.create_table(
        'documents',
        sa.Column('id', sa.UUID(as_uuid=True), nullable=False, primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column('chatbot_id', sa.UUID(as_uuid=True), nullable=False),
        sa.Column('tenant_id', sa.UUID(as_uuid=True), nullable=False),
        sa.Column('filename', sa.Text(), nullable=False),
        sa.Column('mime_type', sa.Text(), nullable=False),
        sa.Column('s3_key', sa.Text(), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='pending'),
        sa.Column('page_count', sa.Integer()),
        sa.Column('error_reason', sa.Text()),
        sa.Column('deleted_at', sa.DateTime(timezone=True)),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    )

    op.create_table(
        'chunks',
        sa.Column('id', sa.UUID(as_uuid=True), nullable=False, primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column('document_id', sa.UUID(as_uuid=True), nullable=False),
        sa.Column('chatbot_id', sa.UUID(as_uuid=True), nullable=False),
        sa.Column('tenant_id', sa.UUID(as_uuid=True), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('token_count', sa.Integer(), nullable=False),
        sa.Column('chunk_index', sa.Integer(), nullable=False),
        sa.Column('embedding', Vector(1536), nullable=False),
        sa.Column('embedding_model', sa.Text(), nullable=False),
    )

    op.create_table(
        'conversations',
        sa.Column('id', sa.UUID(as_uuid=True), nullable=False, primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column('chatbot_id', sa.UUID(as_uuid=True), nullable=False),
        sa.Column('session_id', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    )

    op.create_table(
        'messages',
        sa.Column('id', sa.UUID(as_uuid=True), nullable=False, primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column('conversation_id', sa.UUID(as_uuid=True), nullable=False),
        sa.Column('role', sa.String(length=10), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('source_chunk_ids', JSONB()),
        sa.Column('tokens_used', sa.Integer()),
        sa.Column('no_answer', sa.Boolean(), server_default='false', nullable=False),
        sa.Column('prompt_version', sa.Text()),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    )

    op.create_foreign_key('chatbots_tenant_fkey', 'chatbots', 'tenants', ['tenant_id'], ['id'])
    op.create_foreign_key('documents_chatbot_fkey', 'documents', 'chatbots', ['chatbot_id'], ['id'])
    op.create_foreign_key('documents_tenant_fkey', 'documents', 'tenants', ['tenant_id'], ['id'])
    op.create_foreign_key('chunks_document_fkey', 'chunks', 'documents', ['document_id'], ['id'])
    op.create_foreign_key('chunks_chatbot_fkey', 'chunks', 'chatbots', ['chatbot_id'], ['id'])
    op.create_foreign_key('chunks_tenant_fkey', 'chunks', 'tenants', ['tenant_id'], ['id'])
    op.create_foreign_key('conversations_chatbot_fkey', 'conversations', 'chatbots', ['chatbot_id'], ['id'])
    op.create_foreign_key('messages_conversation_fkey', 'messages', 'conversations', ['conversation_id'], ['id'])

    op.create_unique_constraint('tenants_clerk_user_uq', 'tenants', ['clerk_user_id'])
    op.create_unique_constraint('conversations_chatbot_session_uq', 'conversations', ['chatbot_id', 'session_id'])

    op.create_index('chatbots_tenant_idx', 'chatbots', ['tenant_id'])
    op.create_index('documents_chatbot_idx', 'documents', ['chatbot_id'])
    op.create_index('chunks_chatbot_idx', 'chunks', ['chatbot_id'])
    op.create_index('messages_conversation_idx', 'messages', ['conversation_id', 'created_at'])

    op.execute("""
        CREATE INDEX IF NOT EXISTS chunks_embedding_hnsw
        ON chunks USING hnsw (embedding vector_cosine_ops)
        WITH (m=16, ef_construction=64);
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS chunks_chatbot_embedding_idx
        ON chunks (chatbot_id);
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS chunks_chatbot_embedding_idx;")
    op.execute("DROP INDEX IF EXISTS chunks_embedding_hnsw;")

    op.drop_index('messages_conversation_idx', table_name='messages')
    op.drop_index('chunks_chatbot_idx', table_name='chunks')
    op.drop_index('documents_chatbot_idx', table_name='documents')
    op.drop_index('chatbots_tenant_idx', table_name='chatbots')

    op.drop_constraint('conversations_chatbot_session_uq', 'conversations', type_='unique')
    op.drop_constraint('tenants_clerk_user_uq', 'tenants', type_='unique')

    op.drop_constraint('messages_conversation_fkey', 'messages', type_='foreignkey')
    op.drop_constraint('conversations_chatbot_fkey', 'conversations', type_='foreignkey')
    op.drop_constraint('chunks_tenant_fkey', 'chunks', type_='foreignkey')
    op.drop_constraint('chunks_chatbot_fkey', 'chunks', type_='foreignkey')
    op.drop_constraint('chunks_document_fkey', 'chunks', type_='foreignkey')
    op.drop_constraint('documents_tenant_fkey', 'documents', type_='foreignkey')
    op.drop_constraint('documents_chatbot_fkey', 'documents', type_='foreignkey')
    op.drop_constraint('chatbots_tenant_fkey', 'chatbots', type_='foreignkey')

    op.drop_table('messages')
    op.drop_table('conversations')
    op.drop_table('chunks')
    op.drop_table('documents')
    op.drop_table('chatbots')
    op.drop_table('tenants')
