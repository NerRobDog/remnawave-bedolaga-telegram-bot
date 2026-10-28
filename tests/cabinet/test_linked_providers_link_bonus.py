"""Флаг ``link_bonus`` в ``GET /cabinet/auth/account/linked-providers``: для баннера «привяжите второй способ входа».

Поле добавлено к ответу, который кабинет и так грузит для экрана способов входа; прежние поля не тронуты.
``available`` истинно только когда привязка прямо сейчас дала бы дни.
"""

from __future__ import annotations

import pytest

from app.cabinet.routes import account_linking
from app.config import settings
from app.database.models import Subscription, SubscriptionStatus
from app.services import auth_link_bonus_service as service
from tests.cabinet.passkey_harness import open_harness


pytestmark = [pytest.mark.usefixtures('passkey_settings'), pytest.mark.asyncio]

URL = '/cabinet/auth/account/linked-providers'
EXTRA = [(account_linking.router, '/cabinet')]
DAYS = 7


@pytest.fixture(autouse=True)
def bonus_on(monkeypatch) -> None:
    monkeypatch.setattr(settings, 'AUTH_LINK_BONUS_ENABLED', True)
    monkeypatch.setattr(settings, 'AUTH_LINK_BONUS_DAYS', DAYS)
    monkeypatch.setattr(settings, 'AUTH_LINK_BONUS_REQUIRES_PAID', True)


async def _flag(h, user) -> dict:
    response = await h.client.get(URL, headers=h.auth(user))
    assert response.status_code == 200, response.text
    return response.json()['link_bonus']


async def test_a_paying_account_with_no_extra_method_sees_the_bonus(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis, extra_routers=EXTRA) as h:
        user, _ = await h.make_paying_user(telegram_id=555001)
        flag = await _flag(h, user)

    assert flag == {'available': True, 'days': DAYS}


async def test_the_response_keeps_its_old_fields_and_adds_exactly_one(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis, extra_routers=EXTRA) as h:
        user, _ = await h.make_paying_user(telegram_id=555001)
        body = (await h.client.get(URL, headers=h.auth(user))).json()

    assert set(body) == {'providers', 'link_bonus'}
    assert set(body['link_bonus']) == {'available', 'days'}
    assert {item['provider'] for item in body['providers']} >= {'telegram'}
    assert next(item for item in body['providers'] if item['provider'] == 'telegram')['linked'] is True


async def test_the_flag_is_off_when_the_feature_is_disabled(monkeypatch, fake_redis) -> None:
    monkeypatch.setattr(settings, 'AUTH_LINK_BONUS_ENABLED', False)
    async with open_harness(monkeypatch, fake_redis, extra_routers=EXTRA) as h:
        user, _ = await h.make_paying_user()
        flag = await _flag(h, user)

    assert flag == {'available': False, 'days': 0}


async def test_the_flag_goes_off_once_a_passkey_is_registered_and_the_bonus_taken(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis, extra_routers=EXTRA) as h:
        user, _ = await h.make_paying_user()
        assert (await _flag(h, user))['available'] is True

        _, registered = await h.register(user)

        assert registered.status_code == 201
        flag = await _flag(h, user)

    assert flag['available'] is False


async def test_the_flag_is_off_when_a_provider_is_already_linked(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis, extra_routers=EXTRA) as h:
        user, _ = await h.make_paying_user(google_id='sub-1')
        flag = await _flag(h, user)

    assert flag['available'] is False


async def test_the_flag_is_off_for_an_account_without_payment_history(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis, extra_routers=EXTRA) as h:
        user, _ = await h.make_paying_user(paid=False)
        flag = await _flag(h, user)

    assert flag['available'] is False


async def test_the_flag_is_on_without_payment_history_when_not_required(monkeypatch, fake_redis) -> None:
    monkeypatch.setattr(settings, 'AUTH_LINK_BONUS_REQUIRES_PAID', False)
    async with open_harness(monkeypatch, fake_redis, extra_routers=EXTRA) as h:
        user, _ = await h.make_paying_user(paid=False)
        flag = await _flag(h, user)

    assert flag == {'available': True, 'days': DAYS}


async def test_the_flag_is_off_for_a_trial_only_account(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis, extra_routers=EXTRA) as h:
        user, sub_id = await h.make_paying_user()
        subscription = await h.db.get(Subscription, sub_id)
        subscription.is_trial = True
        subscription.status = SubscriptionStatus.TRIAL.value
        await h.db.commit()
        flag = await _flag(h, user)

    assert flag['available'] is False


async def test_the_flag_is_off_when_there_is_nothing_to_extend(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis, extra_routers=EXTRA) as h:
        user, sub_id = await h.make_paying_user()
        subscription = await h.db.get(Subscription, sub_id)
        subscription.status = SubscriptionStatus.EXPIRED.value
        await h.db.commit()
        flag = await _flag(h, user)

    assert flag['available'] is False


async def test_a_failure_computing_the_flag_never_breaks_the_list(monkeypatch, fake_redis) -> None:
    async def _boom(*args, **kwargs):
        raise RuntimeError('db hiccup')

    monkeypatch.setattr(service, 'get_link_bonus_status', _boom)
    async with open_harness(monkeypatch, fake_redis, extra_routers=EXTRA) as h:
        user, _ = await h.make_paying_user()
        response = await h.client.get(URL, headers=h.auth(user))

    assert response.status_code == 200
    assert response.json()['link_bonus'] == {'available': False, 'days': 0}
    assert response.json()['providers']
