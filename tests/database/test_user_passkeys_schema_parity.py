"""Свежая установка и обновлённая обязаны прийти к одной схеме ``user_passkeys``.

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

from app.database.models import Base, UserPasskey


VERSIONS = pathlib.Path(__file__).resolve().parents[2] / 'migrations/alembic/versions'
MIGRATION = '0128_user_passkeys.py'
TABLE = 'user_passkeys'


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
    Base.metadata.create_all(engine, tables=[UserPasskey.__table__], checkfirst=True)
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


def test_columns_match(both) -> None:
    fresh, upgraded = both
    fresh_cols = {c['name'] for c in fresh.get_columns(TABLE)}
    upgraded_cols = {c['name'] for c in upgraded.get_columns(TABLE)}
    assert fresh_cols == upgraded_cols
    assert {'id', 'user_id', 'credential_id', 'public_key', 'sign_count', 'device_type', 'backed_up'} <= fresh_cols
    assert {'transports', 'name', 'created_at', 'last_used_at'} <= fresh_cols


def test_column_types_and_nullability_match(both) -> None:
    fresh, upgraded = both
    as_map = lambda inspector: {  # noqa: E731
        c['name']: (str(c['type']).upper(), c['nullable']) for c in inspector.get_columns(TABLE)
    }
    assert as_map(fresh) == as_map(upgraded)


def test_indexes_and_unique_constraints_match(both) -> None:
    fresh, upgraded = both

    def shape(inspector):
        indexes = {(i['name'], tuple(i['column_names']), bool(i['unique'])) for i in inspector.get_indexes(TABLE)}
        uniques = {tuple(u['column_names']) for u in inspector.get_unique_constraints(TABLE)}
        return indexes, uniques

    fresh_shape, upgraded_shape = shape(fresh), shape(upgraded)
    assert fresh_shape == upgraded_shape
    unique_columns = {cols for _, cols, unique in fresh_shape[0] if unique} | fresh_shape[1]
    assert ('credential_id',) in unique_columns, 'credential_id обязан быть уникальным: вход ищет ключ по нему'
    assert any(cols == ('user_id',) for _, cols, _ in fresh_shape[0]), 'нужен индекс по user_id'


def test_foreign_key_cascades_with_user(both) -> None:
    for inspector, label in zip(both, ('свежая', 'обновлённая'), strict=True):
        fks = inspector.get_foreign_keys(TABLE)
        assert [(fk['constrained_columns'], fk['referred_table']) for fk in fks] == [(['user_id'], 'users')], label
        assert fks[0]['options'].get('ondelete') == 'CASCADE', f'{label}: удаление пользователя обязано чистить ключи'


def test_downgrade_drops_table(tmp_path) -> None:
    engine = _upgraded(tmp_path / 'roundtrip.db')
    module = _load_migration()
    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            module.downgrade()
    assert TABLE not in sa.inspect(engine).get_table_names()


def test_upgrade_is_idempotent(tmp_path) -> None:
    engine = _upgraded(tmp_path / 'twice.db')
    module = _load_migration()
    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            module.upgrade()
    assert TABLE in sa.inspect(engine).get_table_names()
