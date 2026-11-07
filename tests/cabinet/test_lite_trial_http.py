"""Лёгкий триал в кабинете: ``GET/POST /cabinet/subscription/trial`` на настоящем приложении.

Бюджет — в Redis в памяти, БД — in-memory SQLite, зависимость пользователя не подменяется.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import delete, select

from app.cabinet.routes.subscription_modules.purchase import router as purchase_router
from app.config import settings
from app.database.models import Subscription, TrialIdentityLedger, User
from app.services import lite_trial_budget as budget
from app.utils.cache import cache
from tests.cabinet.passkey_harness import open_harness


pytestmark = [pytest.mark.usefixtures('passkey_settings'), pytest.mark.asyncio]

TRIAL = '/cabinet/subscription/trial'
EXTRA = [(purchase_router, '/cabinet/subscription')]
NOT_FOR_TYPE = 'Trial is not available for your account type'


@pytest.fixture(autouse=True)
def _lite_on(monkeypatch, fake_redis):
    monkeypatch.setattr(settings, 'TRIAL_LITE_ENABLED', True)
    monkeypatch.setattr(settings, 'TRIAL_DISABLED_FOR', 'email')  # как на проде
    monkeypatch.setattr(settings, 'TRIAL_LITE_DURATION_HOURS', 24)
    monkeypatch.setattr(settings, 'TRIAL_LITE_TRAFFIC_LIMIT_GB', 2)
    monkeypatch.setattr(settings, 'TRIAL_LITE_DEVICE_LIMIT', 1)
    monkeypatch.setattr(settings, 'TRIAL_LITE_DAILY_LIMIT', 30)
    monkeypatch.setattr(settings, 'TRIAL_LITE_PER_IP_DAILY_LIMIT', 1)
    monkeypatch.setattr(settings, 'TRIAL_DURATION_DAYS', 14)
    monkeypatch.setattr(settings, 'TRIAL_TRAFFIC_LIMIT_GB', 1000)
    monkeypatch.setattr(settings, 'TRIAL_DEVICE_LIMIT', 3)
    monkeypatch.setattr(settings, 'TRIAL_PAYMENT_ENABLED', False)
    monkeypatch.setattr(settings, 'CABINET_TRUSTED_PROXIES', '127.0.0.1')


def _headers(h, user, ip: str = '203.0.113.10') -> dict[str, str]:
    return {**h.auth(user), 'X-Forwarded-For': ip}


async def _google_user(h, n: int = 1, **fields):
    values = dict(
        auth_type='google',
        google_id=f'google-sub-{n}',
        email=f'person{n}@example.com',
        email_verified=True,
    )
    values.update(fields)
    return await h.make_user(**values)


async def _get(h, user, ip: str = '203.0.113.10'):
    return await h.client.get(TRIAL, headers=_headers(h, user, ip))


async def _post(h, user, ip: str = '203.0.113.10'):
    return await h.client.post(TRIAL, headers=_headers(h, user, ip), json={})


async def _subscriptions(h) -> list[Subscription]:
    result = await h.db.execute(select(Subscription).execution_options(populate_existing=True))
    return list(result.scalars())


async def _ledger_size(h) -> int:
    return len((await h.db.execute(select(TrialIdentityLedger.id))).all())


# -- лёгкий триал для аккаунта без Telegram ---------------------------------


async def test_info_offers_the_lite_trial_to_an_account_without_telegram(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis, extra_routers=EXTRA) as h:
        user = await _google_user(h)
        response = await _get(h, user)

    body = response.json()
    assert response.status_code == 200, response.text
    assert body['is_available'] is True
    assert body['kind'] == 'lite'
    assert body['duration_hours'] == 24
    assert body['traffic_limit_gb'] == 2
    assert body['device_limit'] == 1
    assert body['requires_payment'] is False
    assert body['price_kopeks'] == 0
    assert body['reason_unavailable'] is None


async def test_activation_creates_a_24h_2gb_1device_subscription(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis, extra_routers=EXTRA) as h:
        user = await _google_user(h)
        response = await _post(h, user)
        subscriptions = await _subscriptions(h)
        used = await budget.used_today()
        ledger = await _ledger_size(h)

    assert response.status_code == 200, response.text
    assert len(subscriptions) == 1
    created = subscriptions[0]
    assert created.is_trial is True
    assert created.status == 'active'
    assert abs((created.end_date - created.start_date) - timedelta(hours=24)) < timedelta(seconds=1)
    assert created.traffic_limit_gb == 2
    assert created.device_limit == 1
    assert created.tariff_id is None
    assert used == 1
    assert ledger == 2, 'google sub и подтверждённая почта'


async def test_prod_setting_disabled_for_email_does_not_block_the_lite_trial(monkeypatch, fake_redis) -> None:
    """TRIAL_DISABLED_FOR=email закрывает полный триал без Telegram, но не лёгкий."""
    async with open_harness(monkeypatch, fake_redis, extra_routers=EXTRA) as h:
        user = await _google_user(h)
        assert (await _post(h, user)).status_code == 200

    monkeypatch.setattr(settings, 'TRIAL_LITE_ENABLED', False)
    async with open_harness(monkeypatch, fake_redis, extra_routers=EXTRA) as h:
        other = await _google_user(h, n=2)
        closed = await _get(h, other, ip='203.0.113.77')

    assert closed.json()['is_available'] is False
    assert closed.json()['reason_unavailable'] == NOT_FOR_TYPE


async def test_the_lite_trial_is_given_once_per_account(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis, extra_routers=EXTRA) as h:
        user = await _google_user(h)
        first = await _post(h, user)
        second = await _post(h, user, ip='203.0.113.99')
        info = await _get(h, user, ip='203.0.113.99')
        used = await budget.used_today()

    assert first.status_code == 200
    assert second.status_code == 400
    assert second.json()['detail'] == 'You already have an active subscription'
    assert info.json()['reason_unavailable'] == 'You already have an active subscription'
    assert used == 1, 'отказ повторной попытки не тратит бюджет'


# -- общий дневной лимит -----------------------------------------------------


async def test_global_daily_limit_closes_the_trial_for_everyone(monkeypatch, fake_redis) -> None:
    monkeypatch.setattr(settings, 'TRIAL_LITE_DAILY_LIMIT', 2)
    async with open_harness(monkeypatch, fake_redis, extra_routers=EXTRA) as h:
        users = [await _google_user(h, n=i) for i in range(1, 4)]
        first = await _post(h, users[0], ip='198.51.100.1')
        second = await _post(h, users[1], ip='198.51.100.2')
        third = await _post(h, users[2], ip='198.51.100.3')
        info = await _get(h, users[2], ip='198.51.100.3')
        subscriptions = await _subscriptions(h)

    assert (first.status_code, second.status_code) == (200, 200)
    assert third.status_code == 429
    assert third.json()['detail']['code'] == 'daily_limit_reached'
    assert info.json()['is_available'] is False
    assert info.json()['reason_unavailable'] == 'daily_limit_reached'
    assert len(subscriptions) == 2


async def test_zero_daily_limit_disables_the_lite_trial(monkeypatch, fake_redis) -> None:
    monkeypatch.setattr(settings, 'TRIAL_LITE_DAILY_LIMIT', 0)
    async with open_harness(monkeypatch, fake_redis, extra_routers=EXTRA) as h:
        user = await _google_user(h)
        info = await _get(h, user)
        response = await _post(h, user)

    assert info.json()['is_available'] is False
    assert info.json()['reason_unavailable'] == 'not_available'
    assert response.status_code == 400
    assert response.json()['detail']['code'] == 'not_available'


# -- лимит на IP -------------------------------------------------------------


async def test_one_ip_gets_one_lite_trial_a_day(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis, extra_routers=EXTRA) as h:
        first_user = await _google_user(h, n=1)
        second_user = await _google_user(h, n=2)
        third_user = await _google_user(h, n=3)
        first = await _post(h, first_user, ip='203.0.113.5')
        second = await _post(h, second_user, ip='203.0.113.5')
        info = await _get(h, second_user, ip='203.0.113.5')
        elsewhere = await _post(h, third_user, ip='203.0.113.6')

    assert first.status_code == 200
    assert second.status_code == 429
    assert second.json()['detail']['code'] == 'ip_limit_reached'
    assert info.json()['reason_unavailable'] == 'ip_limit_reached'
    assert elsewhere.status_code == 200


async def test_the_real_client_ip_is_taken_from_the_trusted_proxy(monkeypatch, fake_redis) -> None:
    """Подмена X-Forwarded-For не-прокси клиентом не даёт новый IP."""
    monkeypatch.setattr(settings, 'CABINET_TRUSTED_PROXIES', '10.9.9.9')  # peer 127.0.0.1 — не прокси
    async with open_harness(monkeypatch, fake_redis, extra_routers=EXTRA) as h:
        first_user = await _google_user(h, n=1)
        second_user = await _google_user(h, n=2)
        first = await _post(h, first_user, ip='203.0.113.5')
        second = await _post(h, second_user, ip='203.0.113.6')

    assert first.status_code == 200
    assert second.status_code == 429, 'заголовок проигнорирован: оба запроса с адреса 127.0.0.1'


# -- журнал личностей --------------------------------------------------------


async def test_a_new_account_with_the_same_google_sub_gets_no_second_trial(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis, extra_routers=EXTRA) as h:
        old = await _google_user(h, n=1)
        assert (await _post(h, old, ip='198.51.100.1')).status_code == 200
        # аккаунт удалён и заведён заново теми же Google-данными
        await h.db.execute(delete(Subscription))
        await h.db.execute(delete(User).where(User.id == old.id))
        await h.db.commit()
        reborn = await _google_user(h, n=1, email='fresh-address@example.org')

        info = await _get(h, reborn, ip='198.51.100.2')
        response = await _post(h, reborn, ip='198.51.100.2')
        subscriptions = await _subscriptions(h)
        used = await budget.used_today()

    assert info.json()['is_available'] is False
    assert info.json()['reason_unavailable'] == 'identity_already_used'
    assert response.status_code == 400
    assert response.json()['detail']['code'] == 'identity_already_used'
    assert subscriptions == []
    assert used == 1, 'отказ журнала вернул занятый слот бюджета'


async def test_a_gmail_variant_is_the_same_identity(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis, extra_routers=EXTRA) as h:
        first = await h.make_user(
            auth_type='email', email='john.smith@gmail.com', email_verified=True, password_hash='x'
        )
        assert (await _post(h, first, ip='198.51.100.1')).status_code == 200
        variant = await h.make_user(
            auth_type='email', email='JohnSmith+promo@googlemail.com', email_verified=True, password_hash='x'
        )

        response = await _post(h, variant, ip='198.51.100.2')

    assert response.status_code == 400
    assert response.json()['detail']['code'] == 'identity_already_used'


# -- сбои --------------------------------------------------------------------


async def test_a_failed_activation_returns_the_budget_slot_and_the_ledger_rows(monkeypatch, fake_redis) -> None:
    import app.cabinet.routes.subscription_modules.purchase as purchase_module

    real = purchase_module.create_trial_subscription
    calls = {'n': 0}

    async def flaky(*args, **kwargs):
        calls['n'] += 1
        if calls['n'] == 1:
            raise RuntimeError('database went away')
        return await real(*args, **kwargs)

    monkeypatch.setattr(purchase_module, 'create_trial_subscription', flaky)
    async with open_harness(monkeypatch, fake_redis, extra_routers=EXTRA) as h:
        user = await _google_user(h)
        with pytest.raises(RuntimeError):
            await _post(h, user)
        used_after_failure = await budget.used_today()
        ledger_after_failure = await _ledger_size(h)
        retry = await _post(h, user)

    assert used_after_failure == 0
    assert ledger_after_failure == 0
    assert retry.status_code == 200, retry.text


async def test_redis_down_fails_closed_and_creates_nothing(monkeypatch, fake_redis) -> None:
    monkeypatch.setattr(cache, '_connected', False)
    async with open_harness(monkeypatch, fake_redis, extra_routers=EXTRA) as h:
        user = await _google_user(h)
        info = await _get(h, user)
        response = await _post(h, user)
        subscriptions = await _subscriptions(h)
        ledger = await _ledger_size(h)

    assert info.json()['is_available'] is False
    assert info.json()['reason_unavailable'] == 'not_available'
    assert response.status_code == 400
    assert response.json()['detail']['code'] == 'not_available'
    assert subscriptions == []
    assert ledger == 0


# -- аккаунт с Telegram: полный триал ---------------------------------------


async def test_an_account_with_telegram_still_gets_the_full_trial(monkeypatch, fake_redis) -> None:
    monkeypatch.setattr(settings, 'TRIAL_DISABLED_FOR', 'none')
    async with open_harness(monkeypatch, fake_redis, extra_routers=EXTRA) as h:
        user = await h.make_user(telegram_id=777, auth_type='telegram')
        info = await _get(h, user)
        response = await _post(h, user)
        subscriptions = await _subscriptions(h)
        used = await budget.used_today()
        ledger = await _ledger_size(h)

    assert info.json()['kind'] == 'full'
    assert info.json()['duration_days'] == 14
    assert info.json()['duration_hours'] == 14 * 24
    assert info.json()['traffic_limit_gb'] == 1000
    assert response.status_code == 200, response.text
    created = subscriptions[0]
    assert abs((created.end_date - created.start_date) - timedelta(days=14)) < timedelta(seconds=1)
    assert (created.traffic_limit_gb, created.device_limit) == (1000, 3)
    assert used == 0, 'полный триал бюджет лёгких не тратит'
    assert ledger == 1, 'но личность (tg) в журнал записана'


async def test_full_trial_is_refused_when_the_identity_is_already_in_the_ledger(monkeypatch, fake_redis) -> None:
    """Личность, получившая лёгкий триал, потом привязала Telegram: второго (полного) триала нет."""
    monkeypatch.setattr(settings, 'TRIAL_DISABLED_FOR', 'none')
    async with open_harness(monkeypatch, fake_redis, extra_routers=EXTRA) as h:
        lite_user = await _google_user(h, n=1)
        assert (await _post(h, lite_user, ip='198.51.100.1')).status_code == 200
        await h.db.execute(delete(Subscription))
        await h.db.execute(delete(User).where(User.id == lite_user.id))
        await h.db.commit()
        linked = await _google_user(h, n=1, telegram_id=555)

        response = await _post(h, linked, ip='198.51.100.2')

    assert response.status_code == 400
    assert response.json()['detail']['code'] == 'identity_already_used'


async def test_disabled_for_all_closes_the_trial_in_every_mode(monkeypatch, fake_redis) -> None:
    monkeypatch.setattr(settings, 'TRIAL_DISABLED_FOR', 'all')
    async with open_harness(monkeypatch, fake_redis, extra_routers=EXTRA) as h:
        user = await _google_user(h)
        info = await _get(h, user)
        response = await _post(h, user)

    assert info.json()['is_available'] is False
    assert info.json()['reason_unavailable'] == NOT_FOR_TYPE
    assert info.json()['kind'] is None
    assert response.status_code == 400
    assert response.json()['detail'] == NOT_FOR_TYPE


# -- флаг выключен: поведение прежнее ---------------------------------------


@pytest.mark.parametrize('disabled_for', ['none', 'email', 'telegram', 'all'])
async def test_with_the_flag_off_nothing_changes(monkeypatch, fake_redis, disabled_for) -> None:
    monkeypatch.setattr(settings, 'TRIAL_LITE_ENABLED', False)
    monkeypatch.setattr(settings, 'TRIAL_DISABLED_FOR', disabled_for)
    async with open_harness(monkeypatch, fake_redis, extra_routers=EXTRA) as h:
        user = await _google_user(h)
        info = await _get(h, user)
        response = await _post(h, user)
        subscriptions = await _subscriptions(h)
        touched_redis = fake_redis.keys_with_prefix('lite_trial:')
        ledger = await _ledger_size(h)

    closed = settings.is_trial_disabled_for_user('google')
    body = info.json()
    if closed:
        assert body['is_available'] is False
        assert body['reason_unavailable'] == NOT_FOR_TYPE
        assert response.status_code == 400
        assert subscriptions == []
    else:
        assert body['is_available'] is True
        assert body['kind'] == 'full'
        assert (body['duration_days'], body['traffic_limit_gb'], body['device_limit']) == (14, 1000, 3)
        assert response.status_code == 200
        created = subscriptions[0]
        assert abs((created.end_date - created.start_date) - timedelta(days=14)) < timedelta(seconds=1)
    assert touched_redis == []
    assert ledger == 0, 'без флага журнал и бюджет не используются'
