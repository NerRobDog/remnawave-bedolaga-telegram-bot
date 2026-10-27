"""passkey (WebAuthn) в кабинете: таблицы user_passkeys и user_recovery_codes

Revision ID: 0138
Revises: 0137
Create Date: 2026-10-27

Один пользователь — до нескольких ключей. ``credential_id`` уникален глобально:
вход по discoverable credential находит ключ именно по нему. Приватной части ключа
у сервера нет, хранится только публичный COSE-ключ и счётчик подписей.
Удаление пользователя каскадно удаляет его ключи.

``user_passkeys.user_handle`` — случайный userHandle аккаунта, созданного самим
passkey (id пользователя на момент регистрации ещё нет); у остальных NULL.

``user_recovery_codes`` — одноразовые коды входа для аккаунта без почты и Telegram.
Хранится только HMAC-SHA256 кода под перцем, открытый код — никогда.

Миграция не выкатывалась в прод, поэтому дополнена на месте. Каждый шаг идемпотентен:
стенд, где успела пройти первая версия этой миграции, догоняется недостающим.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0138'
down_revision: Union[str, None] = '0137'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _columns(table: str) -> set[str]:
    return {column['name'] for column in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    if 'user_passkeys' not in _tables():
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
            sa.Column('user_handle', sa.LargeBinary(), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint('credential_id', name='uq_user_passkeys_credential_id'),
        )
        op.create_index('ix_user_passkeys_id', 'user_passkeys', ['id'])
        op.create_index('ix_user_passkeys_user_id', 'user_passkeys', ['user_id'])
    elif 'user_handle' not in _columns('user_passkeys'):
        op.add_column('user_passkeys', sa.Column('user_handle', sa.LargeBinary(), nullable=True))

    if 'user_recovery_codes' not in _tables():
        op.create_table(
            'user_recovery_codes',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
            sa.Column('code_hash', sa.String(64), nullable=False),
            sa.Column('used_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint('code_hash', name='uq_user_recovery_codes_code_hash'),
        )
        op.create_index('ix_user_recovery_codes_id', 'user_recovery_codes', ['id'])
        op.create_index('ix_user_recovery_codes_user_id', 'user_recovery_codes', ['user_id'])


def downgrade() -> None:
    if 'user_recovery_codes' in _tables():
        op.drop_table('user_recovery_codes')
    if 'user_passkeys' in _tables():
        op.drop_table('user_passkeys')
