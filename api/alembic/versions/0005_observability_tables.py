"""observability_tables

Revision ID: 0005
Revises: 0004
Create Date: 2026-05-10

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Cost rates table
    op.create_table(
        'cost_rates',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('provider', sa.Text(), nullable=False),
        sa.Column('model', sa.Text(), nullable=False),
        sa.Column('direction', sa.Text(), nullable=False),
        sa.Column('price_per_1m_tokens', postgresql.NUMERIC(precision=16, scale=6), nullable=False),
        sa.Column('updated_at', sa.TIMESTAMP(), nullable=False),
        sa.Column('deleted_at', sa.TIMESTAMP(), nullable=True),
    )
    op.create_unique_constraint(
        'uq_cost_rates',
        'cost_rates', ['provider', 'model', 'direction', 'deleted_at']
    )

    # Observation logs table
    op.create_table(
        'observation_logs',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('tenant_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('tenants.id', ondelete='CASCADE'), nullable=False),
        sa.Column('chatbot_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('chatbots.id', ondelete='SET NULL'), nullable=True),
        sa.Column('provider', sa.Text(), nullable=False),
        sa.Column('model', sa.Text(), nullable=False),
        sa.Column('phase', sa.Text(), nullable=False),
        sa.Column('direction', sa.Text(), nullable=False),
        sa.Column('tokens', sa.Integer(), nullable=False),
        sa.Column('cost_usd', postgresql.NUMERIC(precision=16, scale=8), nullable=False),
        sa.Column('latency_ms', sa.Integer(), nullable=False),
        sa.Column('request_id', sa.Text(), nullable=False),
        sa.Column('chunk_count', sa.Integer(), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.TIMESTAMP(), nullable=False),
        sa.Column('deleted_at', sa.TIMESTAMP(), nullable=True),
    )

    # Indexes for dashboard queries
    op.create_index(
        'ix_obs_logs_tenant_chatbot_created',
        'observation_logs', ['tenant_id', 'chatbot_id', 'created_at']
    )
    op.create_index(
        'ix_obs_logs_tenant_phase_created',
        'observation_logs', ['tenant_id', 'phase', 'created_at']
    )
    op.create_index(
        'ix_obs_logs_tenant_created',
        'observation_logs', ['tenant_id', 'created_at']
    )


def downgrade() -> None:
    op.drop_index('ix_obs_logs_tenant_created')
    op.drop_index('ix_obs_logs_tenant_phase_created')
    op.drop_index('ix_obs_logs_tenant_chatbot_created')
    op.drop_table('observation_logs')
    op.drop_table('cost_rates')
