"""support assist: cards table and ticket_messages.is_system

Revision ID: 0142
Revises: 0141
Create Date: 2026-11-07

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0142'
down_revision: Union[str, None] = '0141'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'ticket_messages',
        sa.Column('is_system', sa.Boolean(), nullable=False, server_default=sa.text('false')),
    )
    op.create_table(
        'support_assist_cards',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('ticket_id', sa.Integer(), sa.ForeignKey('tickets.id', ondelete='CASCADE'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('trigger', sa.String(20), nullable=False),
        sa.Column('facts', postgresql.JSONB(), nullable=False),
        sa.Column('flags', postgresql.JSONB(), nullable=False),
        sa.Column('card', postgresql.JSONB(), nullable=True),
        sa.Column('model', sa.String(100), nullable=True),
        sa.Column('input_tokens', sa.Integer(), nullable=True),
        sa.Column('output_tokens', sa.Integer(), nullable=True),
        sa.Column('cache_read_tokens', sa.Integer(), nullable=True),
        sa.Column('cost_usd', sa.Numeric(10, 5), nullable=True),
        sa.Column('status', sa.String(20), nullable=False, server_default='shown'),
        sa.Column('final_text', sa.Text(), nullable=True),
        sa.Column('admin_message_id', sa.BigInteger(), nullable=True),
        sa.Column('auto_ask_question_ids', postgresql.JSONB(), nullable=True),
        sa.Column('auto_ask_sent_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('decided_by', sa.BigInteger(), nullable=True),
        sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index('ix_support_assist_cards_id', 'support_assist_cards', ['id'])
    op.create_index('ix_support_assist_cards_ticket_id', 'support_assist_cards', ['ticket_id'])
    op.create_index(
        'uq_support_assist_cards_auto_ask',
        'support_assist_cards',
        ['ticket_id'],
        unique=True,
        postgresql_where=sa.text('auto_ask_sent_at IS NOT NULL'),
    )


def downgrade() -> None:
    op.drop_index('uq_support_assist_cards_auto_ask', table_name='support_assist_cards')
    op.drop_index('ix_support_assist_cards_ticket_id', table_name='support_assist_cards')
    op.drop_index('ix_support_assist_cards_id', table_name='support_assist_cards')
    op.drop_table('support_assist_cards')
    op.drop_column('ticket_messages', 'is_system')
