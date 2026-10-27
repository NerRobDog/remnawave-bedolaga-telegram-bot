"""SeverPay: пользователь кабинета без Telegram не должен получить 'None' в идентификаторах."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

import app.services.payment.severpay as severpay_module
from app.config import settings
from app.services.payment.severpay import SeverPayPaymentMixin


@pytest.fixture
def anyio_backend() -> str:
    return 'asyncio'


class _Session:
    async def commit(self) -> None: ...


async def _create(monkeypatch: pytest.MonkeyPatch, user: Any, email: str | None) -> tuple[dict, AsyncMock]:
    monkeypatch.setattr(settings, 'SEVERPAY_MIN_AMOUNT_KOPEKS', 100, raising=False)
    monkeypatch.setattr(settings, 'SEVERPAY_MAX_AMOUNT_KOPEKS', 10_000_000, raising=False)
    monkeypatch.setattr(settings, 'SEVERPAY_LIFETIME', 60, raising=False)
    monkeypatch.setattr(settings, 'SEVERPAY_CURRENCY', 'RUB', raising=False)
    monkeypatch.setattr(type(settings), 'is_severpay_enabled', lambda self: True)

    create_payment = AsyncMock(return_value={'url': 'https://pay.example/x', 'id': 1, 'uid': 'u'})
    monkeypatch.setattr(severpay_module.severpay_service, 'create_payment', create_payment)

    payment_module = SimpleNamespace(get_user_by_id=AsyncMock(return_value=user))
    crud_module = SimpleNamespace(create_severpay_payment=AsyncMock(return_value=SimpleNamespace(id=7)))
    real_import = severpay_module.import_module
    monkeypatch.setattr(
        severpay_module,
        'import_module',
        lambda name: (
            {'app.services.payment_service': payment_module, 'app.database.crud.severpay': crud_module}.get(name)
            or real_import(name)
        ),
    )

    result = await SeverPayPaymentMixin().create_severpay_payment(
        _Session(), user_id=42, amount_kopeks=50_000, email=email
    )
    assert result is not None
    return result, create_payment


@pytest.mark.anyio
async def test_email_only_user_gets_internal_id_not_none(monkeypatch: pytest.MonkeyPatch) -> None:
    user = SimpleNamespace(id=42, telegram_id=None, email='a@example.com')
    result, create_payment = await _create(monkeypatch, user, email=None)

    kwargs = create_payment.await_args.kwargs
    assert 'None' not in result['order_id']
    assert result['order_id'].startswith('sp42_')
    assert kwargs['client_id'] == '42'
    assert 'None' not in kwargs['client_email']


@pytest.mark.anyio
async def test_email_only_user_uses_own_email_when_available(monkeypatch: pytest.MonkeyPatch) -> None:
    user = SimpleNamespace(id=42, telegram_id=None, email='a@example.com')
    _, create_payment = await _create(monkeypatch, user, email=None)

    assert create_payment.await_args.kwargs['client_email'] == 'a@example.com'


@pytest.mark.anyio
async def test_telegram_user_keeps_telegram_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    user = SimpleNamespace(id=42, telegram_id=555, email=None)
    result, create_payment = await _create(monkeypatch, user, email=None)

    assert result['order_id'].startswith('sp555_')
    assert create_payment.await_args.kwargs['client_id'] == '555'
    assert create_payment.await_args.kwargs['client_email'] == '555@telegram.org'
