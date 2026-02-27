"""repair missing advertising campaign tariff columns in legacy databases

Revision ID: 0014
Revises: 0013
Create Date: 2026-02-27
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0014'
down_revision: Union[str, None] = '0013'
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
    if _has_table('advertising_campaigns'):
        if not _has_column('advertising_campaigns', 'tariff_id'):
            op.add_column('advertising_campaigns', sa.Column('tariff_id', sa.Integer(), nullable=True))
        if not _has_column('advertising_campaigns', 'tariff_duration_days'):
            op.add_column('advertising_campaigns', sa.Column('tariff_duration_days', sa.Integer(), nullable=True))

    if _has_table('advertising_campaign_registrations'):
        if not _has_column('advertising_campaign_registrations', 'tariff_id'):
            op.add_column('advertising_campaign_registrations', sa.Column('tariff_id', sa.Integer(), nullable=True))
        if not _has_column('advertising_campaign_registrations', 'tariff_duration_days'):
            op.add_column(
                'advertising_campaign_registrations',
                sa.Column('tariff_duration_days', sa.Integer(), nullable=True),
            )


def downgrade() -> None:
    # Repair migration, no automatic downgrade.
    pass
