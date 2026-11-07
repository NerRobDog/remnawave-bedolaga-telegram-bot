"""add abroad mode columns to subscriptions

Revision ID: 0141
Revises: 0140
Create Date: 2026-10-30

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0141'
down_revision: Union[str, None] = '0140'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'subscriptions',
        sa.Column('abroad_mode', sa.Boolean(), nullable=False, server_default=sa.text('false')),
    )
    op.add_column('subscriptions', sa.Column('abroad_home_squad_uuid', sa.String(255), nullable=True))


def downgrade() -> None:
    op.drop_column('subscriptions', 'abroad_home_squad_uuid')
    op.drop_column('subscriptions', 'abroad_mode')
