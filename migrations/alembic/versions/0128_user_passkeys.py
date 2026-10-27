"""passkey (WebAuthn) в кабинете: таблица user_passkeys

Revision ID: 0128
Revises: 0127
Create Date: 2026-10-27

Один пользователь — до нескольких ключей. ``credential_id`` уникален глобально:
вход по discoverable credential находит ключ именно по нему. Приватной части ключа
у сервера нет, хранится только публичный COSE-ключ и счётчик подписей.
Удаление пользователя каскадно удаляет его ключи.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0128'
down_revision: Union[str, None] = '0127'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    if 'user_passkeys' in _tables():
        return
    op.create_table(
        'user_passkeys',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('credential_id', sa.LargeBinary(), nullable=False),
        sa.Column('public_key', sa.LargeBinary(), nullable=False),
        sa.Column('sign_count', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('device_type', sa.String(32), nullable=False),
        sa.Column('backed_up', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('transports', sa.String(255), nullable=True),
        sa.Column('name', sa.String(100), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint('credential_id', name='uq_user_passkeys_credential_id'),
    )
    op.create_index('ix_user_passkeys_id', 'user_passkeys', ['id'])
    op.create_index('ix_user_passkeys_user_id', 'user_passkeys', ['user_id'])


def downgrade() -> None:
    if 'user_passkeys' in _tables():
        op.drop_table('user_passkeys')
