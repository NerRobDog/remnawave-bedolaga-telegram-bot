"""tickets.diagnostics: answers of the mandatory form before a ticket

Revision ID: 0143
Revises: 0142
Create Date: 2026-11-07

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0143'
down_revision: Union[str, None] = '0142'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('tickets', sa.Column('diagnostics', sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column('tickets', 'diagnostics')
