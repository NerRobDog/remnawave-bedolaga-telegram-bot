"""auth_link_bonus: one bonus per account for linking a second sign-in method

Revision ID: 0140
Revises: 0139
Create Date: 2026-10-28

The table records that an account already received the bonus days for its first linked
passkey / OAuth provider. ``user_id`` is UNIQUE, so the database itself refuses a second
grant under concurrent requests; deleting the user removes the row. The step is idempotent:
it creates the table only when it is missing, so it is safe to re-run.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0140'
down_revision: Union[str, None] = '0139'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


TABLE = 'auth_link_bonus'


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    if TABLE in _tables():
        return
    op.create_table(
        TABLE,
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('method', sa.String(32), nullable=False),
        sa.Column('granted_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint('user_id', name='uq_auth_link_bonus_user_id'),
    )
    op.create_index('ix_auth_link_bonus_id', TABLE, ['id'])


def downgrade() -> None:
    if TABLE in _tables():
        op.drop_table(TABLE)
