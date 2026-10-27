"""Свежая установка и обновлённая обязаны прийти к одной схеме ``user_passkeys`` и ``user_recovery_codes``.

Свежая база создаётся ``Base.metadata.create_all`` по модели, обновлённая — миграцией
0128. Забытая колонка или индекс в миграции ломает вход по passkey только на
обновлённых установках.
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.database.models import Base, UserPasskey, UserRecoveryCode


VERSIONS = pathlib.Path(__file__).resolve().parents[2] / 'migrations/alembic/versions'
MIGRATION = '0128_user_passkeys.py'
TABLE = 'user_passkeys'
RECOVERY_TABLE = 'user_recovery_codes'
TABLES = (TABLE, RECOVERY_TABLE)


def _load_migration():
    spec = importlib.util.spec_from_file_location('0128', VERSIONS / MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _create_users(conn) -> None:
    conn.execute(sa.text('CREATE TABLE users (id INTEGER PRIMARY KEY)'))


def _fresh(path: pathlib.Path):
    engine = sa.create_engine(f'sqlite:///{path}')
    with engine.begin() as conn:
        _create_users(conn)
    Base.metadata.create_all(engine, tables=[UserPasskey.__table__, UserRecoveryCode.__table__], checkfirst=True)
    return engine


def _upgraded(path: pathlib.Path):
    engine = sa.create_engine(f'sqlite:///{path}')
    with engine.begin() as conn:
        _create_users(conn)
    module = _load_migration()
    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            module.upgrade()
    return engine


@pytest.fixture
def both(tmp_path):
    return sa.inspect(_fresh(tmp_path / 'fresh.db')), sa.inspect(_upgraded(tmp_path / 'upgraded.db'))


@pytest.mark.parametrize('table', TABLES)
def test_columns_match(both, table) -> None:
    fresh, upgraded = both
    fresh_cols = {c['name'] for c in fresh.get_columns(table)}
    upgraded_cols = {c['name'] for c in upgraded.get_columns(table)}
    assert fresh_cols == upgraded_cols
    if table == TABLE:
        assert {'id', 'user_id', 'credential_id', 'public_key', 'sign_count', 'device_type', 'backed_up'} <= fresh_cols
        assert {'transports', 'name', 'created_at', 'last_used_at', 'user_handle'} <= fresh_cols
    else:
        assert fresh_cols == {'id', 'user_id', 'code_hash', 'used_at', 'created_at'}


@pytest.mark.parametrize('table', TABLES)
def test_column_types_and_nullability_match(both, table) -> None:
    fresh, upgraded = both
    as_map = lambda inspector: {  # noqa: E731
        c['name']: (str(c['type']).upper(), c['nullable']) for c in inspector.get_columns(table)
    }
    assert as_map(fresh) == as_map(upgraded)


def test_user_handle_is_optional_binary(both) -> None:
    """Старые ключи handle не хранят (он выводится из user_id), новые passkey-аккаунты хранят случайный."""
    for inspector in both:
        column = next(c for c in inspector.get_columns(TABLE) if c['name'] == 'user_handle')
        assert column['nullable'] is True
        assert 'BLOB' in str(column['type']).upper() or 'BINARY' in str(column['type']).upper()


def _shape(inspector, table):
    indexes = {(i['name'], tuple(i['column_names']), bool(i['unique'])) for i in inspector.get_indexes(table)}
    uniques = {tuple(u['column_names']) for u in inspector.get_unique_constraints(table)}
    return indexes, uniques


@pytest.mark.parametrize('table', TABLES)
def test_indexes_and_unique_constraints_match(both, table) -> None:
    fresh, upgraded = both
    fresh_shape, upgraded_shape = _shape(fresh, table), _shape(upgraded, table)
    assert fresh_shape == upgraded_shape
    unique_columns = {cols for _, cols, unique in fresh_shape[0] if unique} | fresh_shape[1]
    if table == TABLE:
        assert ('credential_id',) in unique_columns, 'credential_id обязан быть уникальным: вход ищет ключ по нему'
    else:
        assert ('code_hash',) in unique_columns, 'вход по коду ищет строку по хэшу: он обязан быть уникальным'
    assert any(cols == ('user_id',) for _, cols, _ in fresh_shape[0]), 'нужен индекс по user_id'


@pytest.mark.parametrize('table', TABLES)
def test_foreign_key_cascades_with_user(both, table) -> None:
    for inspector, label in zip(both, ('свежая', 'обновлённая'), strict=True):
        fks = inspector.get_foreign_keys(table)
        assert [(fk['constrained_columns'], fk['referred_table']) for fk in fks] == [(['user_id'], 'users')], label
        assert fks[0]['options'].get('ondelete') == 'CASCADE', f'{label}: удаление пользователя обязано чистить строки'


def test_downgrade_drops_both_tables(tmp_path) -> None:
    engine = _upgraded(tmp_path / 'roundtrip.db')
    module = _load_migration()
    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            module.downgrade()
    assert not set(TABLES) & set(sa.inspect(engine).get_table_names())


def test_upgrade_is_idempotent(tmp_path) -> None:
    engine = _upgraded(tmp_path / 'twice.db')
    module = _load_migration()
    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            module.upgrade()
    assert set(TABLES) <= set(sa.inspect(engine).get_table_names())


def test_upgrade_completes_a_database_that_ran_the_first_version_of_0128(tmp_path) -> None:
    """0128 успели применить на стенде до появления user_handle и recovery-кодов: догнать, не падая."""
    engine = sa.create_engine(f'sqlite:///{tmp_path / "early.db"}')
    with engine.begin() as conn:
        _create_users(conn)
        conn.execute(
            sa.text(
                'CREATE TABLE user_passkeys (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) '
                'ON DELETE CASCADE, credential_id BLOB NOT NULL, public_key BLOB NOT NULL, '
                'sign_count BIGINT NOT NULL DEFAULT 0, device_type VARCHAR(32) NOT NULL, '
                'backed_up BOOLEAN NOT NULL DEFAULT 0, transports VARCHAR(255), name VARCHAR(100) NOT NULL, '
                'created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, last_used_at DATETIME, '
                'CONSTRAINT uq_user_passkeys_credential_id UNIQUE (credential_id))'
            )
        )
        conn.execute(sa.text('CREATE INDEX ix_user_passkeys_id ON user_passkeys (id)'))
        conn.execute(sa.text('CREATE INDEX ix_user_passkeys_user_id ON user_passkeys (user_id)'))
    module = _load_migration()
    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            module.upgrade()

    inspector = sa.inspect(engine)
    assert 'user_handle' in {c['name'] for c in inspector.get_columns(TABLE)}
    assert RECOVERY_TABLE in inspector.get_table_names()
