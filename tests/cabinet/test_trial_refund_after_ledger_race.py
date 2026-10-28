"""Платный полный триал: журнал личностей отказал уже после списания денег (гонка двух аккаунтов).

Проверка журнала до списания гонку не видит, её разводит только запись под уникальный хэш прямо
перед созданием подписки. Если она отказала после оплаты, деньги возвращаются на баланс
(``add_user_balance`` с ``TransactionType.REFUND``), подписка не создаётся, ответ — 400
``identity_already_used``. Не вернулись деньги — это ``logger.critical``, а ответ тот же.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select

import app.cabinet.routes.subscription_modules.purchase as purchase_module
from app.cabinet.routes.subscription_modules.purchase import router as purchase_router
from app.config import settings
from app.database.models import Subscription, Transaction, TransactionType, User
from app.services.trial_eligibility import LiteTrialReason, LiteTrialRefused
from tests.cabinet.passkey_harness import open_harness


pytestmark = [pytest.mark.usefixtures('passkey_settings'), pytest.mark.asyncio]

TRIAL = '/cabinet/subscription/trial'
EXTRA = [(purchase_router, '/cabinet/subscription')]
PRICE = 15_000
BALANCE = 50_000


@pytest.fixture(autouse=True)
def _paid_full_trial(monkeypatch, fake_redis):
    monkeypatch.setattr(settings, 'TRIAL_LITE_ENABLED', True)
    monkeypatch.setattr(settings, 'TRIAL_DISABLED_FOR', 'none')
    monkeypatch.setattr(settings, 'TRIAL_PAYMENT_ENABLED', True)
    monkeypatch.setattr(settings, 'TRIAL_ACTIVATION_PRICE', PRICE)
    monkeypatch.setattr(settings, 'TRIAL_DURATION_DAYS', 3)

    async def lost_the_race(*_args, **_kwargs):
        raise LiteTrialRefused(LiteTrialReason.IDENTITY_ALREADY_USED)

    monkeypatch.setattr(purchase_module, 'claim_trial', lost_the_race)


async def _balance(h, user_id: int) -> int:
    row = await h.db.execute(
        select(User.balance_kopeks).where(User.id == user_id).execution_options(populate_existing=True)
    )
    return row.scalar_one()


async def _post(h, user):
    return await h.client.post(TRIAL, headers=h.auth(user), json={})


async def test_the_charge_is_refunded_through_add_user_balance_as_a_refund(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis, extra_routers=EXTRA) as h:
        user = await h.make_user(telegram_id=777, auth_type='telegram', balance_kopeks=BALANCE)
        real_add = purchase_module.add_user_balance
        spy = AsyncMock(side_effect=real_add)
        monkeypatch.setattr(purchase_module, 'add_user_balance', spy)

        response = await _post(h, user)

        balance = await _balance(h, user.id)
        subscriptions = (await h.db.execute(select(Subscription))).scalars().all()
        transactions = (await h.db.execute(select(Transaction).order_by(Transaction.id))).scalars().all()

    assert response.status_code == 400, response.text
    assert response.json()['detail']['code'] == 'identity_already_used'
    spy.assert_awaited_once()
    args, kwargs = spy.await_args
    assert args[2] == PRICE
    assert kwargs['transaction_type'] == TransactionType.REFUND
    assert balance == BALANCE, 'после списания и возврата баланс прежний'
    assert subscriptions == []
    assert [(t.type, t.amount_kopeks) for t in transactions] == [
        (TransactionType.SUBSCRIPTION_PAYMENT.value, -PRICE),  # списание хранится со знаком минус
        (TransactionType.REFUND.value, PRICE),
    ]


async def test_a_failed_refund_is_logged_as_critical_and_the_answer_is_unchanged(monkeypatch, fake_redis) -> None:
    critical = MagicMock()
    monkeypatch.setattr(purchase_module, 'logger', MagicMock(critical=critical))
    monkeypatch.setattr(purchase_module, 'add_user_balance', AsyncMock(return_value=False))
    async with open_harness(monkeypatch, fake_redis, extra_routers=EXTRA) as h:
        user = await h.make_user(telegram_id=777, auth_type='telegram', balance_kopeks=BALANCE)

        response = await _post(h, user)

        subscriptions = (await h.db.execute(select(Subscription))).scalars().all()

    assert response.status_code == 400
    assert response.json()['detail']['code'] == 'identity_already_used'
    critical.assert_called_once()
    assert 'refund' in critical.call_args.args[0].lower()
    assert critical.call_args.kwargs['price_kopeks'] == PRICE
    assert subscriptions == []


async def test_a_free_full_trial_refused_by_the_race_refunds_nothing(monkeypatch, fake_redis) -> None:
    monkeypatch.setattr(settings, 'TRIAL_PAYMENT_ENABLED', False)
    spy = AsyncMock(return_value=True)
    monkeypatch.setattr(purchase_module, 'add_user_balance', spy)
    async with open_harness(monkeypatch, fake_redis, extra_routers=EXTRA) as h:
        user = await h.make_user(telegram_id=777, auth_type='telegram', balance_kopeks=BALANCE)

        response = await _post(h, user)

    assert response.status_code == 400
    assert response.json()['detail']['code'] == 'identity_already_used'
    spy.assert_not_awaited()
