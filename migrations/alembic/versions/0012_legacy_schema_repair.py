"""legacy schema repair for cabinet and tariff features

Revision ID: 0012
Revises: 0011
Create Date: 2026-02-27
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0012'
down_revision: Union[str, None] = '0011'
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


def _has_index(table: str, index_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return False
    return index_name in [idx['name'] for idx in inspector.get_indexes(table)]


def upgrade() -> None:
    if not _has_table('payment_method_configs'):
        op.create_table(
            'payment_method_configs',
            sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
            sa.Column('method_id', sa.String(length=50), nullable=False),
            sa.Column('sort_order', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('is_enabled', sa.Boolean(), nullable=False, server_default=sa.text('true')),
            sa.Column('display_name', sa.String(length=255), nullable=True),
            sa.Column('sub_options', sa.JSON(), nullable=True),
            sa.Column('min_amount_kopeks', sa.Integer(), nullable=True),
            sa.Column('max_amount_kopeks', sa.Integer(), nullable=True),
            sa.Column('user_type_filter', sa.String(length=20), nullable=False, server_default='all'),
            sa.Column('first_topup_filter', sa.String(length=10), nullable=False, server_default='any'),
            sa.Column('promo_group_filter_mode', sa.String(length=20), nullable=False, server_default='all'),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=True, server_default=sa.func.now()),
            sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True, server_default=sa.func.now()),
            sa.UniqueConstraint('method_id', name='uq_payment_method_configs_method_id'),
        )
        op.create_index('ix_payment_method_configs_id', 'payment_method_configs', ['id'])
        op.create_index('ix_payment_method_configs_method_id', 'payment_method_configs', ['method_id'])
        op.create_index('ix_payment_method_configs_sort_order', 'payment_method_configs', ['sort_order'])

    if _has_table('payment_method_configs') and not _has_table('payment_method_promo_groups'):
        op.create_table(
            'payment_method_promo_groups',
            sa.Column('payment_method_config_id', sa.Integer(), nullable=False),
            sa.Column('promo_group_id', sa.Integer(), nullable=False),
            sa.ForeignKeyConstraint(
                ['payment_method_config_id'],
                ['payment_method_configs.id'],
                ondelete='CASCADE',
            ),
            sa.ForeignKeyConstraint(
                ['promo_group_id'],
                ['promo_groups.id'],
                ondelete='CASCADE',
            ),
            sa.PrimaryKeyConstraint('payment_method_config_id', 'promo_group_id'),
        )

    if not _has_table('traffic_purchases'):
        op.create_table(
            'traffic_purchases',
            sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
            sa.Column('subscription_id', sa.Integer(), nullable=False),
            sa.Column('traffic_gb', sa.Integer(), nullable=False),
            sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=True, server_default=sa.func.now()),
            sa.ForeignKeyConstraint(['subscription_id'], ['subscriptions.id'], ondelete='CASCADE'),
        )
        op.create_index('ix_traffic_purchases_id', 'traffic_purchases', ['id'])
        op.create_index('ix_traffic_purchases_subscription_id', 'traffic_purchases', ['subscription_id'])
        op.create_index('ix_traffic_purchases_expires_at', 'traffic_purchases', ['expires_at'])

    if _has_table('tariffs'):
        if not _has_column('tariffs', 'max_device_limit'):
            op.add_column('tariffs', sa.Column('max_device_limit', sa.Integer(), nullable=True))
        if not _has_column('tariffs', 'is_daily'):
            op.add_column(
                'tariffs',
                sa.Column('is_daily', sa.Boolean(), nullable=False, server_default=sa.text('false')),
            )
        if not _has_column('tariffs', 'daily_price_kopeks'):
            op.add_column(
                'tariffs',
                sa.Column('daily_price_kopeks', sa.Integer(), nullable=False, server_default='0'),
            )
        if not _has_column('tariffs', 'custom_days_enabled'):
            op.add_column(
                'tariffs',
                sa.Column('custom_days_enabled', sa.Boolean(), nullable=False, server_default=sa.text('false')),
            )
        if not _has_column('tariffs', 'price_per_day_kopeks'):
            op.add_column(
                'tariffs',
                sa.Column('price_per_day_kopeks', sa.Integer(), nullable=False, server_default='0'),
            )
        if not _has_column('tariffs', 'min_days'):
            op.add_column('tariffs', sa.Column('min_days', sa.Integer(), nullable=False, server_default='1'))
        if not _has_column('tariffs', 'max_days'):
            op.add_column('tariffs', sa.Column('max_days', sa.Integer(), nullable=False, server_default='365'))
        if not _has_column('tariffs', 'custom_traffic_enabled'):
            op.add_column(
                'tariffs',
                sa.Column('custom_traffic_enabled', sa.Boolean(), nullable=False, server_default=sa.text('false')),
            )
        if not _has_column('tariffs', 'traffic_price_per_gb_kopeks'):
            op.add_column(
                'tariffs',
                sa.Column('traffic_price_per_gb_kopeks', sa.Integer(), nullable=False, server_default='0'),
            )
        if not _has_column('tariffs', 'min_traffic_gb'):
            op.add_column('tariffs', sa.Column('min_traffic_gb', sa.Integer(), nullable=False, server_default='1'))
        if not _has_column('tariffs', 'max_traffic_gb'):
            op.add_column('tariffs', sa.Column('max_traffic_gb', sa.Integer(), nullable=False, server_default='1000'))
        if not _has_column('tariffs', 'traffic_reset_mode'):
            op.add_column('tariffs', sa.Column('traffic_reset_mode', sa.String(length=20), nullable=True))

    if _has_table('subscriptions'):
        if not _has_column('subscriptions', 'traffic_reset_at'):
            op.add_column('subscriptions', sa.Column('traffic_reset_at', sa.DateTime(timezone=True), nullable=True))
        if not _has_column('subscriptions', 'last_webhook_update_at'):
            op.add_column(
                'subscriptions',
                sa.Column('last_webhook_update_at', sa.DateTime(timezone=True), nullable=True),
            )
        if not _has_column('subscriptions', 'is_daily_paused'):
            op.add_column(
                'subscriptions',
                sa.Column('is_daily_paused', sa.Boolean(), nullable=False, server_default=sa.text('false')),
            )
        if not _has_column('subscriptions', 'last_daily_charge_at'):
            op.add_column(
                'subscriptions',
                sa.Column('last_daily_charge_at', sa.DateTime(timezone=True), nullable=True),
            )
        if _has_table('tariffs') and not _has_column('subscriptions', 'tariff_id'):
            op.add_column('subscriptions', sa.Column('tariff_id', sa.Integer(), nullable=True))
        if not _has_column('subscriptions', 'remnawave_short_uuid'):
            op.add_column('subscriptions', sa.Column('remnawave_short_uuid', sa.String(length=255), nullable=True))
            if not _has_index('subscriptions', 'ix_subscriptions_remnawave_short_uuid'):
                op.create_index(
                    'ix_subscriptions_remnawave_short_uuid',
                    'subscriptions',
                    ['remnawave_short_uuid'],
                )


def downgrade() -> None:
    # Repair migration, no automatic downgrade.
    pass
