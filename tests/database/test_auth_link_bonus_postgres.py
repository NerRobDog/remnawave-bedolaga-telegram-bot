"""Бонус за привязку способа входа под настоящей конкуренцией (PostgreSQL).

SQLite не ждёт ``FOR UPDATE`` и не разводит две транзакции по разным соединениям, поэтому «два запроса
одновременно — одно продление» там проверялось бы для вида. Единственная защита от повтора —
``UNIQUE(auth_link_bonus.user_id)``; здесь она проверяется там, где она и работает.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import (
    AuthLinkBonus,
    PaymentMethod,
    SentNotification,
    Subscription,
    SubscriptionStatus,
    Tariff,
    Transaction,
    TransactionType,
    User,
)
from app.services import auth_link_bonus_service as service
from tests.fixtures.postgres_db import postgres_sessions, wait_for_lock_waiter


pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]

TABLES = (
    AuthLinkBonus.__table__,
    SentNotification.__table__,
    Transaction.__table__,
    Subscription.__table__,
    Tariff.__table__,
    User.__table__,
)
DAYS = 7


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setattr(settings, 'AUTH_LINK_BONUS_ENABLED', True)
    monkeypatch.setattr(settings, 'AUTH_LINK_BONUS_DAYS', DAYS)
    monkeypatch.setattr(settings, 'AUTH_LINK_BONUS_REQUIRES_PAID', True)


async def _seed_paying_user(session: AsyncSession) -> tuple[int, int, datetime]:
    user = User(telegram_id=None, username='payer', status='active', language='ru')
    session.add(user)
    await session.flush()
    session.add(
        Transaction(
            user_id=user.id,
            type=TransactionType.DEPOSIT.value,
            amount_kopeks=10000,
            payment_method=PaymentMethod.YOOKASSA.value,
            is_completed=True,
        )
    )
    end = datetime.now(UTC).replace(microsecond=0) + timedelta(days=30)
    sub = Subscription(
        user_id=user.id,
        status=SubscriptionStatus.ACTIVE.value,
        is_trial=False,
        end_date=end,
        remnawave_short_id=uuid.uuid4().hex[:12],
    )
    session.add(sub)
    await session.commit()
    return user.id, sub.id, end


async def _state(session: AsyncSession, sub_id: int) -> tuple[datetime, int]:
    await session.rollback()  # свежий снимок
    end = (await session.execute(select(Subscription.end_date).where(Subscription.id == sub_id))).scalar_one()
    count = (await session.execute(select(func.count()).select_from(AuthLinkBonus))).scalar_one()
    return end, count


async def _attempt(session: AsyncSession, user_id: int, method: str):
    user = await session.get(User, user_id)
    grant = await service.grant_link_bonus(session, user, method, had_qualifying_method=False)
    await session.commit()
    return grant


async def test_simultaneous_grants_extend_the_subscription_exactly_once(postgres_database) -> None:
    attempts = 6
    async with postgres_sessions(postgres_database, TABLES, count=attempts + 1) as (checker, *sessions):
        user_id, sub_id, end = await _seed_paying_user(checker)

        results = await asyncio.gather(
            *(_attempt(session, user_id, 'passkey' if i % 2 else 'google') for i, session in enumerate(sessions))
        )

        new_end, rows = await _state(checker, sub_id)
        assert sum(grant is not None for grant in results) == 1, 'ровно один запрос получил бонус'
        assert rows == 1
        assert new_end == end + timedelta(days=DAYS), 'подписка продлена один раз'


async def test_the_loser_waits_for_the_winner_and_then_leaves_without_extending(postgres_database) -> None:
    """Детерминированный перекрёст: победитель держит блокировку подписки, проигравший стоит в очереди."""
    async with postgres_sessions(postgres_database, TABLES, count=4) as (checker, watcher, winner, loser):
        user_id, sub_id, end = await _seed_paying_user(checker)
        winner_user = await winner.get(User, user_id)
        loser_user = await loser.get(User, user_id)

        won = await service.grant_link_bonus(winner, winner_user, 'passkey', had_qualifying_method=False)
        assert won is not None  # строка и продление уже в транзакции победителя, но не закоммичены

        async def lose():
            return await service.grant_link_bonus(loser, loser_user, 'google', had_qualifying_method=False)

        async def finish_first():
            await wait_for_lock_waiter(watcher)  # проигравший действительно ждёт, а не опередил по времени
            await winner.commit()

        lost, _ = await asyncio.gather(lose(), finish_first())
        await loser.commit()

        new_end, rows = await _state(checker, sub_id)
        assert lost is None
        assert rows == 1
        assert new_end == end + timedelta(days=DAYS)
        (method,) = (await checker.execute(select(AuthLinkBonus.method))).scalars().all()
        assert method == 'passkey', 'бонус записан за способом победителя'


async def test_a_rolled_back_winner_leaves_the_right_to_the_next_request(postgres_database) -> None:
    """Привязка победителя не прошла (откат): строки нет, право не потеряно, следующий запрос его получит."""
    async with postgres_sessions(postgres_database, TABLES, count=3) as (checker, first, second):
        user_id, sub_id, end = await _seed_paying_user(checker)
        first_user = await first.get(User, user_id)

        assert await service.grant_link_bonus(first, first_user, 'passkey', had_qualifying_method=False) is not None
        await first.rollback()

        assert await _attempt(second, user_id, 'google') is not None
        new_end, rows = await _state(checker, sub_id)
        assert rows == 1 and new_end == end + timedelta(days=DAYS)
