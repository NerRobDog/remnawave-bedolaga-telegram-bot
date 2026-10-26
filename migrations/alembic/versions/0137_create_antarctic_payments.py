"""create antarctic_payments table

Revision ID: 0137
Revises: 0136
Create Date: 2026-10-24

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0137'
down_revision: Union[str, None] = '0136'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'antarctic_payments',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('token', sa.String(64), nullable=False),
        sa.Column('operation_id', sa.String(64), nullable=True),
        sa.Column('user_app_login', sa.String(64), nullable=True),
        sa.Column('amount_kopeks', sa.Integer(), nullable=False),
        sa.Column('amount_usd_cents', sa.Integer(), nullable=False),
        sa.Column('usd_rub_rate', sa.Numeric(12, 4), nullable=False),
        sa.Column('status', sa.String(32), nullable=False, server_default='pending'),
        sa.Column('aw_status', sa.String(32), nullable=True),
        sa.Column('is_paid', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('failure_reason', sa.Text(), nullable=True),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('metadata_json', sa.JSON(), nullable=True),
        sa.Column('callback_payload', sa.JSON(), nullable=True),
        sa.Column('paid_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('transaction_id', sa.Integer(), sa.ForeignKey('transactions.id'), nullable=True),
    )
    op.create_index('ix_antarctic_payments_id', 'antarctic_payments', ['id'])
    op.create_index('ix_antarctic_payments_user_id', 'antarctic_payments', ['user_id'])
    op.create_index('ix_antarctic_payments_token', 'antarctic_payments', ['token'], unique=True)
    op.create_index('ix_antarctic_payments_operation_id', 'antarctic_payments', ['operation_id'], unique=True)


def downgrade() -> None:
    op.drop_table('antarctic_payments')
