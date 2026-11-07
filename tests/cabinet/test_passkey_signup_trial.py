"""Аккаунт, созданный passkey, не получает триал: ни через информацию о триале, ни через его активацию.

Триал выдаётся только явным ``POST /subscription/trial``; а анонимный аккаунт создаётся
за секунды, поэтому без этого гейта под ``TRIAL_DISABLED_FOR=none`` триалы можно было бы
накручивать. Проверяются настоящие ручки кабинета, не только ``Settings``.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.cabinet.routes.subscription_modules.purchase import router as purchase_router
from app.config import settings
from app.database.models import Subscription
from tests.cabinet.passkey_harness import open_harness


pytestmark = [pytest.mark.usefixtures('passkey_settings', 'passkey_signup_settings'), pytest.mark.asyncio]

TRIAL = '/cabinet/subscription/trial'
EXTRA = [(purchase_router, '/cabinet/subscription')]
NOT_FOR_TYPE = 'Trial is not available for your account type'


async def _signed_up(h) -> dict[str, str]:
    _, response = await h.signup()
    assert response.status_code == 200, response.text
    return {'Authorization': f'Bearer {response.json()["access_token"]}'}


@pytest.mark.parametrize('disabled_for', ['none', 'telegram', 'email', 'all'])
async def test_trial_info_says_unavailable_for_a_passkey_account(monkeypatch, fake_redis, disabled_for) -> None:
    monkeypatch.setattr(settings, 'TRIAL_DISABLED_FOR', disabled_for)
    async with open_harness(monkeypatch, fake_redis, extra_routers=EXTRA) as h:
        headers = await _signed_up(h)
        response = await h.client.get(TRIAL, headers=headers)

    assert response.status_code == 200, response.text
    assert response.json()['is_available'] is False
    assert response.json()['reason_unavailable'] == NOT_FOR_TYPE


@pytest.mark.parametrize('disabled_for', ['none', 'telegram', 'email', 'all'])
async def test_trial_activation_is_refused_and_creates_no_subscription(monkeypatch, fake_redis, disabled_for) -> None:
    monkeypatch.setattr(settings, 'TRIAL_DISABLED_FOR', disabled_for)
    async with open_harness(monkeypatch, fake_redis, extra_routers=EXTRA) as h:
        headers = await _signed_up(h)
        response = await h.client.post(TRIAL, headers=headers, json={})
        subscriptions = (await h.db.execute(select(Subscription))).scalars().all()

    assert response.status_code == 400, response.text
    assert response.json()['detail'] == NOT_FOR_TYPE
    assert subscriptions == []


async def test_the_gate_is_specific_to_passkey_accounts(monkeypatch, fake_redis) -> None:
    """Обычный аккаунт под теми же настройками гейт триала проходит: он закрыт именно для passkey."""
    monkeypatch.setattr(settings, 'TRIAL_DISABLED_FOR', 'none')
    async with open_harness(monkeypatch, fake_redis, extra_routers=EXTRA) as h:
        user = await h.make_user(telegram_id=777, auth_type='telegram')
        response = await h.client.get(TRIAL, headers=h.auth(user))

    assert response.status_code == 200, response.text
    assert response.json().get('reason_unavailable') != NOT_FOR_TYPE


async def test_the_explicit_flag_reopens_the_trial_for_passkey_accounts(monkeypatch, fake_redis) -> None:
    monkeypatch.setattr(settings, 'TRIAL_DISABLED_FOR', 'none')
    monkeypatch.setattr(settings, 'PASSKEY_SIGNUP_TRIAL_ENABLED', True)
    async with open_harness(monkeypatch, fake_redis, extra_routers=EXTRA) as h:
        headers = await _signed_up(h)
        response = await h.client.get(TRIAL, headers=headers)

    assert response.status_code == 200, response.text
    assert response.json().get('reason_unavailable') != NOT_FOR_TYPE
