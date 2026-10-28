"""nullable Telegram columns (schema drift) and the trial identity ledger

Revision ID: 0139
Revises: 0138
Create Date: 2026-10-28

Part 1 (drift): ``users.telegram_id`` and ``support_audit_logs.actor_telegram_id`` are
declared nullable by the models (accounts without Telegram), but databases that went
through the old migrations still carry NOT NULL on both, so the first cabinet account
without Telegram fails on insert. The step drops the constraint where it is still
present and skips tables or columns that are already fine, so it is safe to re-run.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0139'
down_revision: Union[str, None] = '0138'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_NULLABLE_COLUMNS: tuple[tuple[str, str], ...] = (
    ('users', 'telegram_id'),
    ('support_audit_logs', 'actor_telegram_id'),
)


def _drop_not_null(table: str, column: str) -> None:
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return
    current = next((c for c in inspector.get_columns(table) if c['name'] == column), None)
    if current is None or current['nullable']:
        return
    with op.batch_alter_table(table) as batch:
        batch.alter_column(column, existing_type=sa.BigInteger(), nullable=True)


def upgrade() -> None:
    for table, column in _NULLABLE_COLUMNS:
        _drop_not_null(table, column)


def downgrade() -> None:
    # The NOT NULL constraint is deliberately not restored: rows without Telegram may exist by now.
    pass
