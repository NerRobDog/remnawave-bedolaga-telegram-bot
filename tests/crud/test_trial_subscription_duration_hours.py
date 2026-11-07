"""create_trial_subscription: срок в часах для лёгкого триала.

Срок подписки хранится датой окончания, поэтому часы ничем не отличаются от дней для
продления, истечения и уведомлений; нужно лишь уметь задать срок короче суток.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.config import settings
from app.database.crud.subscription import create_trial_subscription
from app.database.models import User
from tests.cabinet.passkey_harness import TABLES
from tests.fixtures.sqlite_memory import memory_session


pytestmark = pytest.mark.asyncio

TOLERANCE = timedelta(seconds=1)  # start_date и end_date берутся двумя вызовами now()


def _term(subscription) -> timedelta:
    return subscription.end_date - subscription.start_date


def _close(actual: timedelta, expected: timedelta) -> bool:
    return abs(actual - expected) < TOLERANCE


async def _user(db) -> User:
    user = User(telegram_id=None, username='lite', first_name='Lite', language='ru')
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def test_duration_hours_sets_a_term_shorter_than_a_day(monkeypatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        user = await _user(db)

        subscription = await create_trial_subscription(
            db, user.id, duration_hours=24, traffic_limit_gb=2, device_limit=1, connected_squads=['squad-1']
        )

    assert subscription.is_trial is True
    assert _close(_term(subscription), timedelta(hours=24))
    assert subscription.traffic_limit_gb == 2
    assert subscription.device_limit == 1


async def test_hours_win_over_days(monkeypatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        user = await _user(db)

        subscription = await create_trial_subscription(
            db, user.id, duration_days=14, duration_hours=6, connected_squads=['squad-1']
        )

    assert _close(_term(subscription), timedelta(hours=6))


async def test_without_hours_the_term_is_still_counted_in_days(monkeypatch) -> None:
    monkeypatch.setattr(settings, 'TRIAL_DURATION_DAYS', 3)
    async with memory_session(monkeypatch, TABLES) as db:
        user = await _user(db)

        explicit = await create_trial_subscription(db, user.id, duration_days=5, connected_squads=['squad-1'])
        assert _close(_term(explicit), timedelta(days=5))

    async with memory_session(monkeypatch, TABLES) as db:
        user = await _user(db)

        default = await create_trial_subscription(db, user.id, connected_squads=['squad-1'])
        assert _close(_term(default), timedelta(days=3))


async def test_a_pending_trial_activated_later_also_gets_the_hours(monkeypatch) -> None:
    from datetime import UTC, datetime

    from app.database.models import Subscription, SubscriptionStatus

    async with memory_session(monkeypatch, TABLES) as db:
        user = await _user(db)
        pending = Subscription(
            user_id=user.id,
            status=SubscriptionStatus.PENDING.value,
            is_trial=True,
            end_date=datetime.now(UTC),
            traffic_limit_gb=10,
            device_limit=3,
            connected_squads=[],
        )
        db.add(pending)
        await db.commit()

        subscription = await create_trial_subscription(
            db, user.id, duration_hours=24, traffic_limit_gb=2, device_limit=1, connected_squads=['squad-1']
        )

    assert subscription.id == pending.id
    assert _close(_term(subscription), timedelta(hours=24))
    assert subscription.traffic_limit_gb == 2
    assert subscription.device_limit == 1
