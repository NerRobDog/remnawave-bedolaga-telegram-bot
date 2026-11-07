"""Бюджет лёгких триалов в Redis: общий дневной лимит и лимит на IP.

Резерв атомарный (проверка и увеличение счётчика — одним скриптом), возвращается при сбое
активации и закрыт при недоступном Redis. Сутки — календарный день Europe/Moscow.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.config import settings
from app.services import lite_trial_budget as budget
from app.services.trial_eligibility import LiteTrialReason, LiteTrialRefused
from app.utils.cache import cache


pytestmark = pytest.mark.asyncio

NOON_MSK = datetime(2026, 10, 28, 12, 0, tzinfo=UTC) - timedelta(hours=3)  # 12:00 по Москве = 09:00 UTC


@pytest.fixture(autouse=True)
def _limits(monkeypatch, fake_redis):
    monkeypatch.setattr(settings, 'TRIAL_LITE_ENABLED', True)
    monkeypatch.setattr(settings, 'TRIAL_LITE_DAILY_LIMIT', 30)
    monkeypatch.setattr(settings, 'TRIAL_LITE_PER_IP_DAILY_LIMIT', 1)
    monkeypatch.setattr(budget, '_now', lambda: NOON_MSK)


async def _reserve(ip: str | None = '203.0.113.1'):
    return await budget.reserve_lite_trial(ip)


async def _refused(ip: str | None = '203.0.113.1') -> LiteTrialReason:
    with pytest.raises(LiteTrialRefused) as refused:
        await _reserve(ip)
    return refused.value.reason


async def test_the_30th_reservation_passes_and_the_31st_does_not(monkeypatch) -> None:
    monkeypatch.setattr(settings, 'TRIAL_LITE_PER_IP_DAILY_LIMIT', 0)

    for number in range(30):
        await _reserve(f'198.51.100.{number}')

    assert await _refused('198.51.100.200') == LiteTrialReason.DAILY_LIMIT_REACHED


async def test_one_ip_gets_one_trial_a_day_and_other_ips_are_not_affected() -> None:
    await _reserve('203.0.113.1')

    assert await _refused('203.0.113.1') == LiteTrialReason.IP_LIMIT_REACHED
    await _reserve('203.0.113.2')


async def test_the_per_ip_limit_is_configurable(monkeypatch) -> None:
    monkeypatch.setattr(settings, 'TRIAL_LITE_PER_IP_DAILY_LIMIT', 2)

    await _reserve('203.0.113.1')
    await _reserve('203.0.113.1')

    assert await _refused('203.0.113.1') == LiteTrialReason.IP_LIMIT_REACHED


async def test_zero_per_ip_limit_switches_the_ip_check_off(monkeypatch) -> None:
    monkeypatch.setattr(settings, 'TRIAL_LITE_PER_IP_DAILY_LIMIT', 0)

    await _reserve('203.0.113.1')
    await _reserve('203.0.113.1')
    await _reserve(None)


async def test_a_refusal_by_ip_does_not_spend_the_global_budget(fake_redis) -> None:
    await _reserve('203.0.113.1')
    await _refused('203.0.113.1')
    await _refused('203.0.113.1')

    assert await budget.used_today() == 1


async def test_a_refusal_by_the_global_limit_does_not_spend_the_ip_slot(monkeypatch) -> None:
    monkeypatch.setattr(settings, 'TRIAL_LITE_DAILY_LIMIT', 1)
    await _reserve('203.0.113.1')
    assert await _refused('203.0.113.2') == LiteTrialReason.DAILY_LIMIT_REACHED

    monkeypatch.setattr(settings, 'TRIAL_LITE_DAILY_LIMIT', 5)
    await _reserve('203.0.113.2')


async def test_releasing_a_reservation_returns_both_slots() -> None:
    reservation = await _reserve('203.0.113.1')
    assert await _refused('203.0.113.1') == LiteTrialReason.IP_LIMIT_REACHED

    await reservation.release()

    assert await budget.used_today() == 0
    await _reserve('203.0.113.1')


async def test_release_is_idempotent_and_never_goes_below_zero() -> None:
    first = await _reserve('203.0.113.1')
    await _reserve('203.0.113.2')

    await first.release()
    await first.release()

    assert await budget.used_today() == 1


async def test_a_released_slot_is_available_to_the_next_user_when_the_day_is_full(monkeypatch) -> None:
    monkeypatch.setattr(settings, 'TRIAL_LITE_DAILY_LIMIT', 2)
    monkeypatch.setattr(settings, 'TRIAL_LITE_PER_IP_DAILY_LIMIT', 0)
    first = await _reserve('a')
    await _reserve('b')
    assert await _refused('c') == LiteTrialReason.DAILY_LIMIT_REACHED

    await first.release()

    await _reserve('c')
    assert await _refused('d') == LiteTrialReason.DAILY_LIMIT_REACHED


async def test_parallel_requests_never_exceed_the_limit(monkeypatch) -> None:
    monkeypatch.setattr(settings, 'TRIAL_LITE_PER_IP_DAILY_LIMIT', 0)

    results = await asyncio.gather(*(_reserve(f'10.0.0.{n}') for n in range(60)), return_exceptions=True)

    granted = [r for r in results if not isinstance(r, BaseException)]
    refused = [r for r in results if isinstance(r, LiteTrialRefused)]
    assert len(granted) == 30
    assert len(refused) == 30


async def test_zero_daily_limit_closes_the_lite_trial(monkeypatch) -> None:
    monkeypatch.setattr(settings, 'TRIAL_LITE_DAILY_LIMIT', 0)

    assert await _refused() == LiteTrialReason.NOT_AVAILABLE


async def test_unknown_ip_is_refused_while_the_ip_limit_is_on() -> None:
    assert await _refused(None) == LiteTrialReason.NOT_AVAILABLE
    assert await budget.used_today() == 0


async def test_redis_disconnected_fails_closed(monkeypatch) -> None:
    monkeypatch.setattr(cache, '_connected', False)

    assert await _refused() == LiteTrialReason.NOT_AVAILABLE
    assert await budget.peek_lite_trial('203.0.113.1') == LiteTrialReason.NOT_AVAILABLE


async def test_redis_error_fails_closed(monkeypatch, fake_redis) -> None:
    async def boom(*_args, **_kwargs):
        raise ConnectionError('redis is gone')

    monkeypatch.setattr(fake_redis, 'eval', boom)

    assert await _refused() == LiteTrialReason.NOT_AVAILABLE


async def test_release_survives_a_redis_error(monkeypatch, fake_redis) -> None:
    reservation = await _reserve()

    async def boom(*_args, **_kwargs):
        raise ConnectionError('redis is gone')

    monkeypatch.setattr(fake_redis, 'eval', boom)

    await reservation.release()  # не бросает: сбой возврата только логируется


async def test_the_day_is_the_moscow_calendar_day(monkeypatch) -> None:
    monkeypatch.setattr(settings, 'TRIAL_LITE_DAILY_LIMIT', 1)
    monkeypatch.setattr(settings, 'TRIAL_LITE_PER_IP_DAILY_LIMIT', 0)
    # 23:59:59 по Москве 28 октября = 20:59:59 UTC
    monkeypatch.setattr(budget, '_now', lambda: datetime(2026, 10, 28, 20, 59, 59, tzinfo=UTC))
    await _reserve('a')
    assert await _refused('b') == LiteTrialReason.DAILY_LIMIT_REACHED

    # 00:00:00 по Москве 29 октября = 21:00:00 UTC — новый счётчик
    monkeypatch.setattr(budget, '_now', lambda: datetime(2026, 10, 28, 21, 0, 0, tzinfo=UTC))
    await _reserve('b')


async def test_counters_expire_after_the_day(fake_redis) -> None:
    await _reserve('203.0.113.1')

    keys = fake_redis.keys_with_prefix('lite_trial:')
    assert len(keys) == 2
    for key in keys:
        ttl = fake_redis.ttl(key)
        assert ttl is not None
        assert 0 < ttl <= 27 * 3600, 'счётчик живёт до конца московских суток с небольшим запасом'


async def test_peek_reports_the_reason_without_changing_counters(monkeypatch) -> None:
    assert await budget.peek_lite_trial('203.0.113.1') is None

    await _reserve('203.0.113.1')
    assert await budget.peek_lite_trial('203.0.113.1') == LiteTrialReason.IP_LIMIT_REACHED
    assert await budget.peek_lite_trial('203.0.113.2') is None
    assert await budget.used_today() == 1

    monkeypatch.setattr(settings, 'TRIAL_LITE_DAILY_LIMIT', 1)
    assert await budget.peek_lite_trial('203.0.113.2') == LiteTrialReason.DAILY_LIMIT_REACHED
    assert await budget.used_today() == 1


async def test_the_limit_is_read_at_every_call(monkeypatch) -> None:
    """Правка лимита в админке действует сразу, без перезапуска."""
    monkeypatch.setattr(settings, 'TRIAL_LITE_PER_IP_DAILY_LIMIT', 0)
    monkeypatch.setattr(settings, 'TRIAL_LITE_DAILY_LIMIT', 1)
    await _reserve('a')
    assert await _refused('b') == LiteTrialReason.DAILY_LIMIT_REACHED

    monkeypatch.setattr(settings, 'TRIAL_LITE_DAILY_LIMIT', 2)
    await _reserve('b')


async def test_ipv6_addresses_of_one_network_share_a_slot() -> None:
    """Внутри /64 адрес меняется бесплатно, поэтому лимит считается по сети."""
    await _reserve('2001:db8:1:2::1')

    assert await _refused('2001:db8:1:2:ffff::9') == LiteTrialReason.IP_LIMIT_REACHED
    await _reserve('2001:db8:1:3::1')
