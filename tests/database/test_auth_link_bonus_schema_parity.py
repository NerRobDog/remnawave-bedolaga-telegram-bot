"""Миграция 0140: таблица ``auth_link_bonus`` (одна выдача бонуса за привязку способа входа на аккаунт).

Свежая установка (``create_all`` по модели) и обновлённая база (миграция) обязаны получить одну и ту же
таблицу, а уникальность ``user_id`` — единственная защита от повторной выдачи под гонкой — присутствовать
в обоих случаях. Миграция идемпотентна, как и 0139.
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


VERSIONS = pathlib.Path(__file__).resolve().parents[2] / 'migrations/alembic/versions'
MIGRATION = '0140_auth_link_bonus.py'
TABLE = 'auth_link_bonus'


def _load_migration():
    spec = importlib.util.spec_from_file_location('0140', VERSIONS / MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(engine, step: str) -> None:
    module = _load_migration()
    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            getattr(module, step)()


def _engine(path: pathlib.Path):
    engine = sa.create_engine(f'sqlite:///{path}')
    with engine.begin() as conn:
        conn.execute(sa.text('CREATE TABLE users (id INTEGER PRIMARY KEY)'))
    return engine


def _fresh(path: pathlib.Path):
    from app.database.models import AuthLinkBonus, Base

    engine = _engine(path)
    Base.metadata.create_all(engine, tables=[AuthLinkBonus.__table__], checkfirst=True)
    return engine


def _upgraded(path: pathlib.Path):
    engine = _engine(path)
    _run(engine, 'upgrade')
    return engine


@pytest.fixture
def both(tmp_path):
    return sa.inspect(_fresh(tmp_path / 'fresh.db')), sa.inspect(_upgraded(tmp_path / 'upgraded.db'))


def test_migration_chain_continues_from_0139() -> None:
    module = _load_migration()
    assert module.revision == '0140'
    assert module.down_revision == '0139'


def test_columns_match(both) -> None:
    fresh, upgraded = both
    fresh_cols = {c['name'] for c in fresh.get_columns(TABLE)}
    assert fresh_cols == {c['name'] for c in upgraded.get_columns(TABLE)}
    assert fresh_cols == {'id', 'user_id', 'method', 'granted_at'}


def test_column_types_and_nullability_match(both) -> None:
    fresh, upgraded = both
    as_map = lambda inspector: {  # noqa: E731
        c['name']: (str(c['type']).upper(), c['nullable']) for c in inspector.get_columns(TABLE)
    }
    assert as_map(fresh) == as_map(upgraded)
    columns = as_map(fresh)
    assert columns['user_id'][1] is False
    assert columns['method'] == ('VARCHAR(32)', False)
    assert columns['granted_at'][1] is False


def _shape(inspector):
    indexes = {(i['name'], tuple(i['column_names']), bool(i['unique'])) for i in inspector.get_indexes(TABLE)}
    uniques = {tuple(u['column_names']) for u in inspector.get_unique_constraints(TABLE)}
    return indexes, uniques


def test_indexes_and_unique_constraints_match(both) -> None:
    fresh, upgraded = both
    assert _shape(fresh) == _shape(upgraded)
    indexes, uniques = _shape(fresh)
    unique_columns = {cols for _, cols, unique in indexes if unique} | uniques
    assert ('user_id',) in unique_columns, 'повторная выдача ловится по уникальности user_id'


def test_foreign_key_to_users_cascades_in_both(both) -> None:
    for inspector in both:
        (fk,) = inspector.get_foreign_keys(TABLE)
        assert fk['referred_table'] == 'users'
        assert fk['referred_columns'] == ['id']
        assert fk['constrained_columns'] == ['user_id']
        assert (fk.get('options') or {}).get('ondelete', '').upper() == 'CASCADE'


def test_deleting_the_user_removes_the_row(tmp_path) -> None:
    engine = _upgraded(tmp_path / 'cascade.db')
    with engine.begin() as conn:
        conn.exec_driver_sql('PRAGMA foreign_keys=ON')
        conn.execute(sa.text('INSERT INTO users (id) VALUES (1)'))
        conn.execute(sa.text("INSERT INTO auth_link_bonus (user_id, method) VALUES (1, 'passkey')"))
        conn.execute(sa.text('DELETE FROM users WHERE id = 1'))
        assert conn.execute(sa.text('SELECT COUNT(*) FROM auth_link_bonus')).scalar() == 0


def test_second_row_for_the_same_user_is_refused(tmp_path) -> None:
    engine = _upgraded(tmp_path / 'unique.db')
    with engine.begin() as conn:
        conn.execute(sa.text('INSERT INTO users (id) VALUES (1)'))
        conn.execute(sa.text("INSERT INTO auth_link_bonus (user_id, method) VALUES (1, 'passkey')"))
    with pytest.raises(sa.exc.IntegrityError):
        with engine.begin() as conn:
            conn.execute(sa.text("INSERT INTO auth_link_bonus (user_id, method) VALUES (1, 'google')"))


def test_upgrade_twice_keeps_the_rows(tmp_path) -> None:
    engine = _upgraded(tmp_path / 'twice.db')
    with engine.begin() as conn:
        conn.execute(sa.text('INSERT INTO users (id) VALUES (1)'))
        conn.execute(sa.text("INSERT INTO auth_link_bonus (user_id, method) VALUES (1, 'passkey')"))

    _run(engine, 'upgrade')

    with engine.connect() as conn:
        assert conn.execute(sa.text('SELECT COUNT(*) FROM auth_link_bonus')).scalar() == 1


def test_granted_at_defaults_to_now(tmp_path) -> None:
    engine = _upgraded(tmp_path / 'default.db')
    with engine.begin() as conn:
        conn.execute(sa.text('INSERT INTO users (id) VALUES (1)'))
        conn.execute(sa.text("INSERT INTO auth_link_bonus (user_id, method) VALUES (1, 'passkey')"))

    with engine.connect() as conn:
        assert conn.execute(sa.text('SELECT granted_at FROM auth_link_bonus')).scalar() is not None


def test_downgrade_drops_the_table_and_is_safe_to_repeat(tmp_path) -> None:
    engine = _upgraded(tmp_path / 'roundtrip.db')

    _run(engine, 'downgrade')
    _run(engine, 'downgrade')

    assert TABLE not in sa.inspect(engine).get_table_names()
