"""Слияние аккаунтов не размножает и не теряет факт «бонус за привязку получен».

Поглощённый аккаунт остаётся надгробием со статусом deleted, его строка выдачи не исчезает, но выжившему
она ничего не говорила бы: отвязав и привязав провайдер заново, он получил бы бонус второй раз за одного
человека. Строка поэтому переезжает на выжившего, если у него своей нет.
"""

from __future__ import annotations

import functools
import inspect
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.config import settings
from app.database.models import (
    AuthLinkBonus,
    GraceAccessSessionModel,
    PaymentMethod,
    SentNotification,
    Subscription,
    Tariff,
    Transaction,
    TransactionType,
    User,
    UserPasskey,
)
from app.services import account_merge_service, auth_link_bonus_service as service
from app.services.account_merge_service import execute_merge
from tests.fixtures.sqlite_memory import memory_session
from tests.services.test_account_merge_service import _make_db, _make_user, _patch_remnawave_delete


TABLES = (
    User.__table__,
    UserPasskey.__table__,
    Subscription.__table__,
    Tariff.__table__,
    Transaction.__table__,
    AuthLinkBonus.__table__,
    SentNotification.__table__,
    GraceAccessSessionModel.__table__,
)


def uses_db(test):
    signature = inspect.signature(test)
    params = [p for p in signature.parameters.values() if p.name != 'db']
    if 'monkeypatch' not in signature.parameters:
        params.append(inspect.Parameter('monkeypatch', inspect.Parameter.POSITIONAL_OR_KEYWORD))

    @functools.wraps(test)
    async def wrapper(*args, **kwargs):
        passed = {k: v for k, v in kwargs.items() if k in signature.parameters}
        async with memory_session(kwargs['monkeypatch'], TABLES) as db:
            await test(*args, db=db, **passed)

    wrapper.__signature__ = signature.replace(parameters=params)
    return wrapper


@pytest.fixture(autouse=True)
def _stub_grace_delete_guard(monkeypatch):
    """Тест слияния на «двойнике» БД, как и в test_account_merge_service."""
    monkeypatch.setattr('app.services.grace_access_runtime.ensure_no_open_grace_for_users', AsyncMock())


async def _two_users(db) -> tuple[int, int]:
    primary = User(telegram_id=None, username='primary', status='active')
    secondary = User(telegram_id=None, username='secondary', status='active')
    db.add_all([primary, secondary])
    await db.commit()
    return primary.id, secondary.id


async def _owners(db) -> dict[str, int]:
    rows = (await db.execute(select(AuthLinkBonus.method, AuthLinkBonus.user_id))).all()
    return {method: user_id for method, user_id in rows}


@pytest.mark.asyncio
@uses_db
async def test_the_row_of_the_absorbed_account_moves_to_the_survivor(db) -> None:
    primary_id, secondary_id = await _two_users(db)
    db.add(AuthLinkBonus(user_id=secondary_id, method='google'))
    await db.commit()

    await service.carry_over_on_merge(db, primary_id=primary_id, secondary_id=secondary_id)
    await db.commit()

    assert await _owners(db) == {'google': primary_id}


@pytest.mark.asyncio
@uses_db
async def test_the_survivors_own_row_wins_and_the_other_stays_on_the_tombstone(db) -> None:
    primary_id, secondary_id = await _two_users(db)
    db.add_all(
        [AuthLinkBonus(user_id=primary_id, method='passkey'), AuthLinkBonus(user_id=secondary_id, method='google')]
    )
    await db.commit()

    await service.carry_over_on_merge(db, primary_id=primary_id, secondary_id=secondary_id)
    await db.commit()

    assert await _owners(db) == {'passkey': primary_id, 'google': secondary_id}


@pytest.mark.asyncio
@uses_db
async def test_nothing_moves_when_the_absorbed_account_never_got_a_bonus(db) -> None:
    primary_id, secondary_id = await _two_users(db)

    await service.carry_over_on_merge(db, primary_id=primary_id, secondary_id=secondary_id)
    await db.commit()

    assert await _owners(db) == {}


@pytest.mark.asyncio
@uses_db
async def test_the_survivor_cannot_collect_a_second_bonus_after_the_merge(db, monkeypatch) -> None:
    monkeypatch.setattr(settings, 'AUTH_LINK_BONUS_ENABLED', True)
    monkeypatch.setattr(settings, 'AUTH_LINK_BONUS_REQUIRES_PAID', True)
    primary_id, secondary_id = await _two_users(db)
    db.add(AuthLinkBonus(user_id=secondary_id, method='google'))
    db.add(
        Transaction(
            user_id=primary_id,
            type=TransactionType.DEPOSIT.value,
            amount_kopeks=10000,
            payment_method=PaymentMethod.YOOKASSA.value,
            is_completed=True,
        )
    )
    await db.commit()

    await service.carry_over_on_merge(db, primary_id=primary_id, secondary_id=secondary_id)
    await db.commit()

    primary = await db.get(User, primary_id)
    assert await service.grant_link_bonus(db, primary, 'google', had_qualifying_method=False) is None
    assert (await service.get_link_bonus_status(db, primary)).available is False


@pytest.mark.asyncio
async def test_execute_merge_carries_the_bonus_over(monkeypatch) -> None:
    db = _make_db()
    primary = _make_user(id=1)
    secondary = _make_user(id=2)
    monkeypatch.setattr(account_merge_service, 'get_user_by_id', AsyncMock(side_effect=[primary, secondary]))
    carried = AsyncMock()
    monkeypatch.setattr(account_merge_service, 'carry_link_bonus_over', carried)

    with _patch_remnawave_delete():
        await execute_merge(db, 1, 2)

    carried.assert_awaited_once_with(db, primary_id=1, secondary_id=2)
