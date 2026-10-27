"""Traffic / low-balance alerts must reach cabinet users without Telegram by email."""

import datetime as datetime_module
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import monitoring_service
from app.services.monitoring_service import MonitoringService
from app.services.notification_settings_service import NotificationSettingsService
from app.services.notification_types import NotificationType


@pytest.fixture(autouse=True)
def _notifications_on(monkeypatch):
    monkeypatch.setattr(
        NotificationSettingsService,
        'are_notifications_globally_enabled',
        classmethod(lambda cls: True),
    )
    monkeypatch.setattr(monitoring_service.cache, 'get', AsyncMock(return_value=None))
    monkeypatch.setattr(monitoring_service.cache, 'set', AsyncMock(return_value=True))


def _user(**kw):
    base = dict(
        id=1,
        telegram_id=None,
        email='a@b.c',
        email_verified=True,
        language='ru',
        status='active',
        balance_kopeks=0,
        notification_settings={'balance_low_enabled': True, 'balance_low_threshold': 10000},
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _service(bot=True):
    service = MonitoringService.__new__(MonitoringService)
    service.bot = SimpleNamespace(send_message=AsyncMock()) if bot else None
    return service


def _db_returning(items):
    scalars = MagicMock()
    scalars.all.return_value = items
    result = MagicMock()
    result.scalars.return_value = scalars
    return SimpleNamespace(execute=AsyncMock(return_value=result))


def _sub(user, **kw):
    base = dict(id=10, user=user, traffic_limit_gb=100, traffic_used_gb=90.0)
    base.update(kw)
    return SimpleNamespace(**base)


async def test_traffic_warning_emails_user_without_telegram(monkeypatch):
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(monitoring_service.notification_delivery_service, 'send_notification', send)
    service = _service()

    await service._check_traffic_warnings(_db_returning([_sub(_user())]))

    send.assert_awaited_once()
    assert send.await_args.kwargs['notification_type'] is NotificationType.TRAFFIC_WARNING
    ctx = send.await_args.kwargs['context']
    assert ctx['limit_gb'] == 100
    service.bot.send_message.assert_not_awaited()


async def test_traffic_warning_emailless_works_without_bot(monkeypatch):
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(monitoring_service.notification_delivery_service, 'send_notification', send)

    await _service(bot=False)._check_traffic_warnings(_db_returning([_sub(_user())]))

    send.assert_awaited_once()


async def test_traffic_warning_telegram_user_unchanged(monkeypatch):
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(monitoring_service.notification_delivery_service, 'send_notification', send)
    service = _service()

    await service._check_traffic_warnings(_db_returning([_sub(_user(telegram_id=555))]))

    service.bot.send_message.assert_awaited_once()
    send.assert_not_awaited()


async def test_traffic_warning_skips_user_without_any_channel(monkeypatch):
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(monitoring_service.notification_delivery_service, 'send_notification', send)
    service = _service()

    await service._check_traffic_warnings(_db_returning([_sub(_user(email_verified=False))]))

    send.assert_not_awaited()
    service.bot.send_message.assert_not_awaited()


def _freeze_noon(monkeypatch):
    real = datetime_module.datetime

    class _Noon(real):
        @classmethod
        def now(cls, tz=None):
            return real(2026, 1, 1, 12, 0, tzinfo=tz)

    monkeypatch.setattr(datetime_module, 'datetime', _Noon)


async def test_low_balance_query_does_not_require_telegram_and_emails(monkeypatch):
    _freeze_noon(monkeypatch)
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(monitoring_service.notification_delivery_service, 'send_notification', send)
    service = _service()
    db = _db_returning([_user(balance_kopeks=500)])

    await service._check_low_balance_alerts(db)

    sql = str(db.execute.await_args.args[0]).lower()
    assert 'users.telegram_id is not null or' in sql
    assert 'users.email_verified is true' in sql
    send.assert_awaited_once()
    assert send.await_args.kwargs['notification_type'] is NotificationType.BALANCE_LOW
    service.bot.send_message.assert_not_awaited()


async def test_low_balance_telegram_user_unchanged(monkeypatch):
    _freeze_noon(monkeypatch)
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(monitoring_service.notification_delivery_service, 'send_notification', send)
    service = _service()

    await service._check_low_balance_alerts(_db_returning([_user(telegram_id=7, balance_kopeks=500)]))

    service.bot.send_message.assert_awaited_once()
    send.assert_not_awaited()
