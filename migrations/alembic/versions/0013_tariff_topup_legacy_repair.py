"""repair missing tariff topup columns in legacy databases

Revision ID: 0013
Revises: 0012
Create Date: 2026-02-27
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0013'
down_revision: Union[str, None] = '0012'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(table: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return table in inspector.get_table_names()


def _has_column(table: str, column: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return False
    return column in [c['name'] for c in inspector.get_columns(table)]


def upgrade() -> None:
    if not _has_table('tariffs'):
        return

    if not _has_column('tariffs', 'server_traffic_limits'):
        op.add_column(
            'tariffs',
            sa.Column('server_traffic_limits', sa.JSON(), nullable=True, server_default=sa.text("'{}'::json")),
        )
    if not _has_column('tariffs', 'allow_traffic_topup'):
        op.add_column(
            'tariffs',
            sa.Column('allow_traffic_topup', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        )
    if not _has_column('tariffs', 'traffic_topup_enabled'):
        op.add_column(
            'tariffs',
            sa.Column('traffic_topup_enabled', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        )
    if not _has_column('tariffs', 'traffic_topup_packages'):
        op.add_column(
            'tariffs',
            sa.Column('traffic_topup_packages', sa.JSON(), nullable=True, server_default=sa.text("'{}'::json")),
        )
    if not _has_column('tariffs', 'max_topup_traffic_gb'):
        op.add_column(
            'tariffs',
            sa.Column('max_topup_traffic_gb', sa.Integer(), nullable=False, server_default='0'),
        )


def downgrade() -> None:
    # Repair migration, no automatic downgrade.
    pass
