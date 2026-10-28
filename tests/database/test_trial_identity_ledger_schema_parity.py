"""Миграция 0139: снимает NOT NULL-дрейф у колонок Telegram и заводит журнал личностей триала.

Модели давно объявляют ``users.telegram_id`` и ``support_audit_logs.actor_telegram_id``
необязательными (аккаунт без Telegram), но на базах, прошедших старые миграции, колонки
остались NOT NULL: первый же аккаунт кабинета без Telegram падал на вставке.
Миграция обязана быть идемпотентной и приводить такую базу к схеме модели.
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


VERSIONS = pathlib.Path(__file__).resolve().parents[2] / 'migrations/alembic/versions'
MIGRATION = '0139_trial_identity_ledger.py'


def _load_migration():
    spec = importlib.util.spec_from_file_location('0139', VERSIONS / MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(engine, step: str) -> None:
    module = _load_migration()
    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            getattr(module, step)()


def _drifted(path: pathlib.Path):
    """База, прошедшая старые миграции: обе колонки Telegram — NOT NULL."""
    engine = sa.create_engine(f'sqlite:///{path}')
    with engine.begin() as conn:
        conn.execute(sa.text('CREATE TABLE users (id INTEGER PRIMARY KEY, telegram_id BIGINT NOT NULL)'))
        conn.execute(sa.text('CREATE UNIQUE INDEX ix_users_telegram_id ON users (telegram_id)'))
        conn.execute(
            sa.text('CREATE TABLE support_audit_logs (id INTEGER PRIMARY KEY, actor_telegram_id BIGINT NOT NULL)')
        )
    return engine


def _nullable(engine, table: str, column: str) -> bool:
    return next(c for c in sa.inspect(engine).get_columns(table) if c['name'] == column)['nullable']


@pytest.mark.parametrize(
    ('table', 'column'),
    [('users', 'telegram_id'), ('support_audit_logs', 'actor_telegram_id')],
)
def test_upgrade_drops_not_null_drift(tmp_path, table, column) -> None:
    engine = _drifted(tmp_path / 'drift.db')
    assert _nullable(engine, table, column) is False

    _run(engine, 'upgrade')

    assert _nullable(engine, table, column) is True


def test_upgrade_keeps_the_unique_index_on_telegram_id(tmp_path) -> None:
    engine = _drifted(tmp_path / 'unique.db')

    _run(engine, 'upgrade')

    indexes = {(i['name'], bool(i['unique'])) for i in sa.inspect(engine).get_indexes('users')}
    assert ('ix_users_telegram_id', True) in indexes


def test_account_without_telegram_can_be_inserted_after_upgrade(tmp_path) -> None:
    engine = _drifted(tmp_path / 'insert.db')
    _run(engine, 'upgrade')

    with engine.begin() as conn:
        conn.execute(sa.text('INSERT INTO users (id, telegram_id) VALUES (1, NULL)'))
        conn.execute(sa.text('INSERT INTO users (id, telegram_id) VALUES (2, NULL)'))

    with engine.connect() as conn:
        assert conn.execute(sa.text('SELECT COUNT(*) FROM users WHERE telegram_id IS NULL')).scalar() == 2


def test_upgrade_is_idempotent_on_an_already_nullable_database(tmp_path) -> None:
    engine = _drifted(tmp_path / 'twice.db')
    _run(engine, 'upgrade')
    _run(engine, 'upgrade')

    assert _nullable(engine, 'users', 'telegram_id') is True
    assert _nullable(engine, 'support_audit_logs', 'actor_telegram_id') is True


def test_upgrade_skips_missing_tables(tmp_path) -> None:
    """Свежая установка без support_audit_logs не должна падать на дрейфовом шаге."""
    engine = sa.create_engine(f'sqlite:///{tmp_path / "bare.db"}')
    with engine.begin() as conn:
        conn.execute(sa.text('CREATE TABLE users (id INTEGER PRIMARY KEY, telegram_id BIGINT)'))

    _run(engine, 'upgrade')

    assert _nullable(engine, 'users', 'telegram_id') is True
