from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.services import monitoring_service
from app.services.monitoring_service import MonitoringService
from app.services.notification_settings_service import NotificationSettingsService


def _service():
    service = MonitoringService.__new__(MonitoringService)
    service.bot = SimpleNamespace(send_message=AsyncMock())
    service._log_monitoring_event = AsyncMock()
    return service


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return SimpleNamespace(all=lambda: list(self._rows))


class _Db:
    def __init__(self, rows):
        self.rows = rows
        self.statements = []

    async def execute(self, stmt, *args, **kwargs):
        self.statements.append(stmt)
        return _Result(self.rows)


def _user(**kw):
    base = {
        'id': 1,
        'telegram_id': 111,
        'language': 'ru',
        'notification_settings': {},
        'has_had_paid_subscription': False,
    }
    base.update(kw)
    return SimpleNamespace(**base)


def _sub(user, **kw):
    base = {
        'id': 10,
        'user': user,
        'is_trial': True,
        'tariff': None,
        'end_date': datetime.now(UTC) - timedelta(hours=1),
    }
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setattr(settings, 'ENABLE_NOTIFICATIONS', True)
    monkeypatch.setattr(settings, 'NOTIFICATION_TRIAL_EXPIRED_OFFER_ENABLED', True)
    monkeypatch.setattr(settings, 'NOTIFICATION_TRIAL_EXPIRED_OFFER_PERCENT', 20)
    monkeypatch.setattr(settings, 'NOTIFICATION_TRIAL_EXPIRED_OFFER_VALID_HOURS', 48)
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', False, raising=False)


# ---------------------------------------------------------------- settings


def test_offer_settings_defaults_and_clamping(monkeypatch):
    assert NotificationSettingsService.is_trial_expired_offer_enabled() is True
    assert NotificationSettingsService.get_trial_expired_offer_percent() == 20
    assert NotificationSettingsService.get_trial_expired_offer_valid_hours() == 48
    monkeypatch.setattr(settings, 'NOTIFICATION_TRIAL_EXPIRED_OFFER_PERCENT', 500)
    monkeypatch.setattr(settings, 'NOTIFICATION_TRIAL_EXPIRED_OFFER_VALID_HOURS', 0)
    assert NotificationSettingsService.get_trial_expired_offer_percent() == 100
    assert NotificationSettingsService.get_trial_expired_offer_valid_hours() == 1


# ---------------------------------------------------------------- trial warning window


@pytest.mark.parametrize(('configured', 'expected'), [(24, 24), (2, 2), (0, 1), (-5, 1), (500, 72)])
def test_trial_warning_hours_clamped(monkeypatch, configured, expected):
    monkeypatch.setattr(settings, 'TRIAL_WARNING_HOURS', configured)
    assert MonitoringService._trial_warning_hours() == expected


async def test_trial_warning_window_uses_setting(monkeypatch):
    monkeypatch.setattr(settings, 'TRIAL_WARNING_HOURS', 24)
    service = _service()
    db = _Db([])
    before = datetime.now(UTC)
    await service._check_trial_expiring_soon(db)
    params = [v for v in db.statements[0].compile().params.values() if isinstance(v, datetime)]
    upper = max(params)
    assert timedelta(hours=23, minutes=59) < upper - before < timedelta(hours=24, minutes=1)


async def test_trial_warning_text_mentions_configured_hours(monkeypatch):
    monkeypatch.setattr(settings, 'TRIAL_WARNING_HOURS', 24)
    sent = {}

    async def fake_send(self, **kwargs):
        sent.update(kwargs)

    monkeypatch.setattr(MonitoringService, '_send_message_with_logo', fake_send)
    service = _service()
    user = _user()
    ok = await service._send_trial_ending_notification(
        user, _sub(user, end_date=datetime.now(UTC) + timedelta(hours=20))
    )
    assert ok is True
    assert 'через 2 часа' not in sent['text']
    assert '24 часа' in sent['text']


async def test_trial_warning_keeps_trial_2h_dedupe_type(monkeypatch):
    monkeypatch.setattr(settings, 'TRIAL_WARNING_HOURS', 24)
    user = _user()
    sub = _sub(user, end_date=datetime.now(UTC) + timedelta(hours=5))
    notification_sent = AsyncMock(return_value=False)
    record = AsyncMock()
    monkeypatch.setattr(monitoring_service, 'notification_sent', notification_sent)
    monkeypatch.setattr(monitoring_service, 'record_notification', record)
    service = _service()
    service._send_trial_ending_notification = AsyncMock(return_value=True)
    await service._check_trial_expiring_soon(_Db([sub]))
    assert notification_sent.await_args.args[3] == 'trial_2h'
    assert record.await_args.args[3] == 'trial_2h'


# ---------------------------------------------------------------- trial ended offer


def _patch_crud(monkeypatch, already_sent=False):
    offer = SimpleNamespace(id=77, expires_at=datetime.now(UTC) + timedelta(hours=48))
    upsert = AsyncMock(return_value=offer)
    sent = AsyncMock(return_value=already_sent)
    record = AsyncMock()
    monkeypatch.setattr(monitoring_service, 'upsert_discount_offer', upsert)
    monkeypatch.setattr(monitoring_service, 'notification_sent', sent)
    monkeypatch.setattr(monitoring_service, 'record_notification', record)
    return upsert, sent, record


async def test_offer_created_sent_and_recorded(monkeypatch):
    upsert, _, record = _patch_crud(monkeypatch)
    user = _user()
    sub = _sub(user)
    service = _service()
    service._send_trial_expired_offer_notification = AsyncMock(return_value=True)

    await service._check_trial_expired_offer(_Db([sub]))

    kwargs = upsert.await_args.kwargs
    assert kwargs['notification_type'] == 'trial_expired_offer'
    assert kwargs['discount_percent'] == 20
    assert kwargs['valid_hours'] == 48
    assert kwargs['effect_type'] == 'percent_discount'
    assert kwargs['user_id'] == user.id and kwargs['subscription_id'] == sub.id
    service._send_trial_expired_offer_notification.assert_awaited_once()
    assert record.await_args.args[3] == 'trial_expired_offer'


async def test_offer_not_resent_when_already_recorded(monkeypatch):
    upsert, _, record = _patch_crud(monkeypatch, already_sent=True)
    user = _user()
    service = _service()
    service._send_trial_expired_offer_notification = AsyncMock(return_value=True)
    await service._check_trial_expired_offer(_Db([_sub(user)]))
    upsert.assert_not_awaited()
    service._send_trial_expired_offer_notification.assert_not_awaited()
    record.assert_not_awaited()


async def test_offer_not_recorded_when_send_fails(monkeypatch):
    _, _, record = _patch_crud(monkeypatch)
    user = _user()
    service = _service()
    service._send_trial_expired_offer_notification = AsyncMock(return_value=False)
    await service._check_trial_expired_offer(_Db([_sub(user)]))
    record.assert_not_awaited()


@pytest.mark.parametrize(
    'case',
    ['no_telegram', 'paid_before', 'daily_tariff', 'prefs_off', 'not_trial', 'older_than_24h', 'in_future'],
)
async def test_offer_skips_ineligible(monkeypatch, case):
    upsert, _, record = _patch_crud(monkeypatch)
    user = _user()
    sub = _sub(user)
    if case == 'no_telegram':
        user.telegram_id = None
    elif case == 'paid_before':
        user.has_had_paid_subscription = True
    elif case == 'daily_tariff':
        sub.tariff = SimpleNamespace(is_daily=True)
    elif case == 'prefs_off':
        user.notification_settings = {'subscription_expiry_enabled': False}
    elif case == 'not_trial':
        sub.is_trial = False
    elif case == 'older_than_24h':
        sub.end_date = datetime.now(UTC) - timedelta(hours=30)
    elif case == 'in_future':
        sub.end_date = datetime.now(UTC) + timedelta(hours=3)
    service = _service()
    service._send_trial_expired_offer_notification = AsyncMock(return_value=True)
    # DB returns the row regardless: the python-side guards must hold on their own.
    await service._check_trial_expired_offer(_Db([sub]))
    upsert.assert_not_awaited()
    service._send_trial_expired_offer_notification.assert_not_awaited()
    record.assert_not_awaited()


async def test_offer_respects_global_and_own_switch(monkeypatch):
    upsert, _, _ = _patch_crud(monkeypatch)
    service = _service()
    service._send_trial_expired_offer_notification = AsyncMock(return_value=True)
    user = _user()

    db = _Db([_sub(user)])
    monkeypatch.setattr(settings, 'ENABLE_NOTIFICATIONS', False)
    await service._check_trial_expired_offer(db)
    assert db.statements == []

    monkeypatch.setattr(settings, 'ENABLE_NOTIFICATIONS', True)
    monkeypatch.setattr(settings, 'NOTIFICATION_TRIAL_EXPIRED_OFFER_ENABLED', False)
    await service._check_trial_expired_offer(db)
    assert db.statements == []
    upsert.assert_not_awaited()


async def test_offer_message_text_and_button(monkeypatch):
    sent = {}

    async def fake_send(self, **kwargs):
        sent.update(kwargs)

    monkeypatch.setattr(MonitoringService, '_send_message_with_logo', fake_send)
    service = _service()
    user = _user()
    sub = _sub(user)
    expires = datetime.now(UTC) + timedelta(hours=48)
    ok = await service._send_trial_expired_offer_notification(user, sub, 20, expires, 77, 48)
    assert ok is True
    assert '20%' in sent['text']
    assert '48' in sent['text']
    callbacks = [b.callback_data for row in sent['reply_markup'].inline_keyboard for b in row]
    assert 'claim_discount_77' in callbacks
    assert 'menu_buy' in callbacks
