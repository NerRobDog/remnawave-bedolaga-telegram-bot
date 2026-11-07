"""Миграция 0143: tickets.diagnostics на PostgreSQL совпадает с моделью, откат колонку убирает."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.database.models import Ticket, User
from tests.fixtures.postgres_db import postgres_engine


MIGRATION = Path('migrations/alembic/versions/0143_ticket_diagnostics.py')


def _migration():
    spec = importlib.util.spec_from_file_location('m0143', MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_revision_chain():
    module = _migration()
    assert (module.revision, module.down_revision) == ('0143', '0142')


def test_model_column_is_nullable_json():
    column = Ticket.__table__.c.diagnostics
    assert column.nullable is True
    assert isinstance(column.type, sa.JSON)


def _run(connection, step: str) -> dict | None:
    module = _migration()
    context = MigrationContext.configure(connection)
    with Operations.context(context):
        getattr(module, step)()
    columns = {c['name']: c for c in sa.inspect(connection).get_columns('tickets')}
    return columns.get('diagnostics')


@pytest.mark.postgres
async def test_upgrade_adds_a_nullable_json_column_and_downgrade_drops_it(postgres_database):
    # Схема тестовой базы создана по моделям и колонка уже есть: сначала откат, иначе миграция
    # упрётся в готовую колонку; в конце возвращаем колонку для остальных тестов.
    async with postgres_engine(postgres_database, [User.__table__, Ticket.__table__]) as engine:
        async with engine.begin() as connection:
            try:
                assert await connection.run_sync(_run, 'downgrade') is None
                added = await connection.run_sync(_run, 'upgrade')
                assert added is not None
                assert added['nullable'] is True
                assert 'json' in str(added['type']).lower()
                assert await connection.run_sync(_run, 'downgrade') is None
            finally:
                present = await connection.run_sync(
                    lambda c: any(x['name'] == 'diagnostics' for x in sa.inspect(c).get_columns('tickets'))
                )
                if not present:
                    await connection.run_sync(_run, 'upgrade')
