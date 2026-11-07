"""Кабинет: пополнение через Antarctic Wallet и статусы в списке ожидающих платежей."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.cabinet.routes import balance as balance_routes
from app.cabinet.schemas.balance import TopUpRequest
from app.config import settings
from app.database.models import PaymentMethod
from app.services.payment_verification_service import PendingPayment


@pytest.fixture(autouse=True)
def _enable(monkeypatch):
    monkeypatch.setattr(settings, 'ANTARCTIC_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'ANTARCTIC_APP_ID', 'app1', raising=False)
    monkeypatch.setattr(settings, 'ANTARCTIC_APP_SECRET', 'secret', raising=False)
    monkeypatch.setattr(
        balance_routes,
        'get_payment_methods',
        AsyncMock(
            return_value=[
                SimpleNamespace(
                    id='antarctic', is_available=True, min_amount_kopeks=10000, max_amount_kopeks=10_000_000
                )
            ]
        ),
    )


def _user():
    return SimpleNamespace(id=7, telegram_id=7, username='payer', language='ru', restriction_topup=False, email=None)


def _patch_service(monkeypatch, result):
    create = AsyncMock(return_value=result)

    class _Service:
        def __init__(self, *_args, **_kwargs):
            pass

        create_antarctic_payment = create

    monkeypatch.setattr(balance_routes, 'PaymentService', _Service)
    return create


async def _topup():
    request = TopUpRequest(amount_kopeks=10000, payment_method='antarctic')
    return await balance_routes.create_topup(request, user=_user(), db=object())


@pytest.mark.asyncio
async def test_topup_returns_pay_page_url(monkeypatch):
    create = _patch_service(
        monkeypatch, {'local_payment_id': 5, 'pay_page_url': 'https://bot.example/antarctic/pay?t=x'}
    )

    response = await _topup()

    assert response.payment_url == 'https://bot.example/antarctic/pay?t=x'
    assert response.payment_id == '5'
    assert create.await_args.kwargs['amount_kopeks'] == 10000
    assert create.await_args.kwargs['user_id'] == 7


@pytest.mark.asyncio
async def test_topup_disabled_is_400(monkeypatch):
    monkeypatch.setattr(settings, 'ANTARCTIC_ENABLED', False, raising=False)
    _patch_service(monkeypatch, None)

    with pytest.raises(HTTPException) as error:
        await _topup()

    assert error.value.status_code == 400
    assert error.value.detail == 'Antarctic Wallet payment method is unavailable'


@pytest.mark.asyncio
@pytest.mark.parametrize('result', [None, {'local_payment_id': 5, 'pay_page_url': None}])
async def test_topup_failure_is_500(monkeypatch, result):
    _patch_service(monkeypatch, result)

    with pytest.raises(HTTPException) as error:
        await _topup()

    assert error.value.status_code == 500
    assert error.value.detail == 'Failed to create Antarctic Wallet payment'


def _record(status: str, *, expires_at: datetime | None = None, is_paid: bool = False) -> PendingPayment:
    return PendingPayment(
        method=PaymentMethod.ANTARCTIC,
        local_id=5,
        identifier='tok',
        amount_kopeks=10000,
        status=status,
        is_paid=is_paid,
        created_at=datetime.now(UTC),
        user=_user(),
        payment=SimpleNamespace(token='tok', expires_at=expires_at),
        expires_at=expires_at,
    )


@pytest.mark.parametrize(
    ('status', 'expected'),
    [
        ('pending', ('⏳', 'Ожидает оплаты')),
        ('success', ('✅', 'Оплачено')),
        ('rejected', ('❌', 'Отклонён')),
        ('failed', ('❌', 'Оплата не прошла')),
        ('expired', ('⌛', 'Истёк')),
        ('amount_mismatch', ('⚠️', 'Несовпадение суммы')),
    ],
)
def test_status_labels(status, expected):
    assert balance_routes._get_status_info(_record(status)) == expected


@pytest.mark.parametrize(('status', 'checkable'), [('pending', True), ('success', False), ('expired', False)])
def test_checkable_only_while_pending(status, checkable):
    assert balance_routes._is_checkable(_record(status)) is checkable


@pytest.fixture
def _pay_page(monkeypatch):
    monkeypatch.setattr(settings, 'WEBHOOK_URL', 'https://bot.example.com', raising=False)
    monkeypatch.setattr(settings, 'ANTARCTIC_PAY_PAGE_PATH', '/antarctic/pay', raising=False)


@pytest.mark.usefixtures('_pay_page')
@pytest.mark.parametrize(
    'expires_at',
    [datetime.now(UTC) + timedelta(minutes=10), None],
    ids=['not-expired', 'no-expiry'],
)
def test_pending_payment_links_to_the_pay_page(expires_at):
    record = _record('pending', expires_at=expires_at)

    assert balance_routes._get_payment_url(record) == 'https://bot.example.com/antarctic/pay/tok'
    assert balance_routes._record_to_response(record).payment_url == 'https://bot.example.com/antarctic/pay/tok'


@pytest.mark.usefixtures('_pay_page')
@pytest.mark.parametrize(
    ('status', 'expires_in', 'is_paid'),
    [
        ('pending', timedelta(minutes=-1), False),
        ('success', timedelta(minutes=10), True),
        ('expired', timedelta(minutes=10), False),
        ('rejected', timedelta(minutes=10), False),
    ],
    ids=['pending-expired', 'paid', 'expired', 'rejected'],
)
def test_closed_or_expired_payment_has_no_link(status, expires_in, is_paid):
    record = _record(status, expires_at=datetime.now(UTC) + expires_in, is_paid=is_paid)

    assert balance_routes._get_payment_url(record) is None


@pytest.mark.usefixtures('_pay_page')
def test_naive_expiry_is_read_as_utc():
    soon = (datetime.now(UTC) + timedelta(minutes=10)).replace(tzinfo=None)
    assert balance_routes._get_payment_url(_record('pending', expires_at=soon)) == (
        'https://bot.example.com/antarctic/pay/tok'
    )
    past = (datetime.now(UTC) - timedelta(minutes=10)).replace(tzinfo=None)
    assert balance_routes._get_payment_url(_record('pending', expires_at=past)) is None
