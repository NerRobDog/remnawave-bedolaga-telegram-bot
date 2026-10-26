"""Antarctic Wallet: создание платежа, сессия мини-приложения, вебхук, сверка и зачисление.

Зачисление проверяется сквозным путём на SQLite: реальный пользователь, баланс и
транзакция, а не моки CRUD — именно там живут двойные зачисления. Клиент AW заменён
заглушкой (``get_antarctic_client`` подменяется в модуле миксина), курс — фиксированный 95.5.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm.attributes import set_committed_value

import app.services.payment.antarctic as aw_module
from app.config import settings
from app.database.models import AntarcticPayment, Base, PaymentMethod, Transaction, User
from app.external.antarctic import AntarcticAPIError, AntarcticIntent, AntarcticTokenError
from app.services.payment.antarctic import (
    ANTARCTIC_STATUS_MAP,
    ANTARCTIC_TERMINAL_FAILURES,
    AntarcticSessionError,
    usd_cents_for,
)
from app.services.payment_service import PaymentService
from tests.fixtures.sqlite_memory import memory_session
from tests.services.user_reminders.test_import_cycle import _graph, _path


TABLES = list(Base.metadata.sorted_tables)

RATE = 95.5
APP_ID = '10000001'
_SAME = object()


def _enable(monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> None:
    values = {
        'ANTARCTIC_ENABLED': True,
        'ANTARCTIC_APP_ID': APP_ID,
        'ANTARCTIC_APP_SECRET': 'sk_test',
        'ANTARCTIC_MIN_AMOUNT_KOPEKS': 10000,
        'ANTARCTIC_MAX_AMOUNT_KOPEKS': 10000000,
        'ANTARCTIC_MARKUP_PERCENT': 0.0,
        'ANTARCTIC_PAYMENT_TTL_MINUTES': 30,
        'ANTARCTIC_REFERRAL_URL': 'https://t.me/antarctic_wallet_bot/app?startapp=ref_1',
        'WEBHOOK_URL': 'https://bot.example.com',
    }
    values.update(overrides)
    for key, value in values.items():
        monkeypatch.setattr(settings, key, value, raising=False)


class StubAW:
    """Заглушка клиента AW: запоминает вызовы, отвечает заданным интентом."""

    def __init__(self, *, status: str = 'pending', operation_id: str = 'op-1', amount: Any = _SAME) -> None:
        self.status = status
        self.operation_id = operation_id
        self.amount = amount
        self.error: Exception | None = None
        self.calls: list[dict[str, Any]] = []

    async def upsert_pay_intent(self, *, user_app_login: str, reference_id: str, amount_usd: str) -> AntarcticIntent:
        self.calls.append({'user_app_login': user_app_login, 'reference_id': reference_id, 'amount_usd': amount_usd})
        if self.error is not None:
            raise self.error
        amount = amount_usd if self.amount is _SAME else self.amount
        return AntarcticIntent(
            operation_id=self.operation_id,
            status=self.status,
            amount=amount,
            failure_reason=None,
            raw={'operationId': self.operation_id, 'status': self.status},
        )

    async def verify_id_token(self, id_token: str) -> str:
        # В тестах idToken и есть sub; 'bad' — токен, не прошедший проверку.
        if id_token == 'bad':
            raise AntarcticTokenError('idToken verification failed')
        return id_token


def _install(monkeypatch: pytest.MonkeyPatch, stub: StubAW | None, *, rate: Any = RATE) -> None:
    monkeypatch.setattr(aw_module, 'get_antarctic_client', lambda: stub)

    async def fake_rate() -> Any:
        if isinstance(rate, Exception):
            raise rate
        return rate

    monkeypatch.setattr(aw_module.currency_converter, 'get_usd_to_rub_rate', fake_rate)


def _service() -> PaymentService:
    service = PaymentService.__new__(PaymentService)  # type: ignore[call-arg]
    service.bot = None
    return service


async def _seed_user(db) -> int:
    user = User(telegram_id=555, username='payer', language='ru', balance_kopeks=0)
    db.add(user)
    await db.commit()
    return user.id


async def _create(db, monkeypatch, stub: StubAW | None = None, *, amount: int = 10000, **enable_overrides):
    """Создаёт платёж; возвращает (user_id, результат create_antarctic_payment)."""
    _enable(monkeypatch, **enable_overrides)
    _install(monkeypatch, stub if stub is not None else StubAW())
    user_id = await _seed_user(db)
    result = await _service().create_antarctic_payment(db, user_id=user_id, amount_kopeks=amount)
    return user_id, result


async def _open(service: PaymentService, db, token: str, login: str = 'login-1') -> dict[str, Any]:
    return await service.start_antarctic_session(db, token=token, id_token=login)


def _event(
    token: str | None,
    *,
    event: str = 'intent.approved',
    status: str | None = 'approved',
    amount: Any = '1.05',
    operation_id: str | None = 'op-1',
    type_: str = 'pay',
) -> dict[str, Any]:
    data: dict[str, Any] = {'type': type_, 'data': {'amount': amount}}
    if token is not None:
        data['reference_id'] = token
    if operation_id is not None:
        data['operation_id'] = operation_id
    if status is not None:
        data['status'] = status
    return {'event': event, 'data': data}


async def _row(db) -> AntarcticPayment:
    return (await db.execute(select(AntarcticPayment).execution_options(populate_existing=True))).scalar_one()


async def _balance(db, user_id: int) -> int:
    await db.rollback()
    return (await db.execute(select(User.balance_kopeks).where(User.id == user_id))).scalar_one()


async def _deposits(db) -> int:
    return (
        await db.execute(
            select(func.count())
            .select_from(Transaction)
            .where(Transaction.payment_method == PaymentMethod.ANTARCTIC.value)
        )
    ).scalar_one()


async def _set_expires(db, when: datetime) -> None:
    await db.execute(update(AntarcticPayment).values(expires_at=when))
    await db.commit()
    db.expire_all()


@pytest.mark.parametrize('module', ['app.services.payment.antarctic', 'app.external.antarctic'])
def test_module_is_not_on_import_cycle(module: str) -> None:
    """CodeQL py/cyclic-import на PR в апстрим: модули провайдера не лежат на кольцах импортов."""
    graph = _graph()
    for neighbour in sorted(graph.get(module, ())):
        chain = _path(graph, neighbour, module)
        assert chain is None, f'{module} -> ' + ' -> '.join(chain)


# --- расчёт суммы в центах ---------------------------------------------------------


def test_usd_cents_rounding():
    assert usd_cents_for(10000, 95.5, 0) == 105
    assert usd_cents_for(10000, 95.5, 5) == 110
    # ровно 100.00 USD: округление вверх не должно накинуть лишний цент
    assert usd_cents_for(9550, 95.5, 0) == 100
    assert usd_cents_for(1, 95.5, 0) == 1


def test_usd_cents_accepts_decimal_and_rejects_nonsense():
    assert usd_cents_for(10000, Decimal('95.5'), Decimal(0)) == 105
    for rate in (0, -1, float('nan'), float('inf')):
        with pytest.raises(ValueError):
            usd_cents_for(10000, rate, 0)
    with pytest.raises(ValueError):
        usd_cents_for(0, 95.5, 0)
    with pytest.raises(ValueError):
        usd_cents_for(10000, 95.5, float('nan'))


def test_status_map_contract():
    assert ANTARCTIC_STATUS_MAP == {
        'pending': ('pending', False),
        'approved': ('success', True),
        'rejected': ('rejected', False),
        'failed': ('failed', False),
        'expired': ('expired', False),
    }
    assert {'rejected', 'failed', 'expired', 'amount_mismatch'} == ANTARCTIC_TERMINAL_FAILURES


def test_session_error_codes_map_to_http_status():
    expected = {
        'invalid_token': 401,
        'not_found': 404,
        'expired': 410,
        'login_mismatch': 409,
        'provider_error': 502,
        'insufficient_balance': 422,
    }
    for code, http_status in expected.items():
        error = AntarcticSessionError(code)
        assert error.code == code
        assert error.http_status == http_status


# --- создание ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_out_of_limits_or_disabled_returns_none(monkeypatch):
    _enable(monkeypatch)
    stub = StubAW()
    _install(monkeypatch, stub)
    async with memory_session(monkeypatch, TABLES) as db:
        user_id = await _seed_user(db)
        service = _service()

        assert await service.create_antarctic_payment(db, user_id=user_id, amount_kopeks=9999) is None
        assert await service.create_antarctic_payment(db, user_id=user_id, amount_kopeks=10000001) is None

        monkeypatch.setattr(settings, 'ANTARCTIC_ENABLED', False)
        assert await service.create_antarctic_payment(db, user_id=user_id, amount_kopeks=10000) is None

        monkeypatch.setattr(settings, 'ANTARCTIC_ENABLED', True)
        monkeypatch.setattr(settings, 'ANTARCTIC_APP_SECRET', None)
        assert await service.create_antarctic_payment(db, user_id=user_id, amount_kopeks=10000) is None

        assert (await db.execute(select(func.count()).select_from(AntarcticPayment))).scalar_one() == 0
    assert stub.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize('rate', [0, -3.0, None, float('nan'), RuntimeError('rates are down')])
async def test_create_without_rate_returns_none(monkeypatch, rate):
    _enable(monkeypatch)
    _install(monkeypatch, StubAW(), rate=rate)
    async with memory_session(monkeypatch, TABLES) as db:
        user_id = await _seed_user(db)
        assert await _service().create_antarctic_payment(db, user_id=user_id, amount_kopeks=10000) is None
        assert (await db.execute(select(func.count()).select_from(AntarcticPayment))).scalar_one() == 0


@pytest.mark.asyncio
async def test_create_for_missing_user_returns_none(monkeypatch):
    """Платёж без пользователя нельзя будет зачислить — не создаём."""
    _enable(monkeypatch)
    _install(monkeypatch, StubAW())
    async with memory_session(monkeypatch, TABLES) as db:
        assert await _service().create_antarctic_payment(db, user_id=4242, amount_kopeks=10000) is None
        assert (await db.execute(select(func.count()).select_from(AntarcticPayment))).scalar_one() == 0


@pytest.mark.asyncio
async def test_create_returns_links(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        before = datetime.now(UTC)
        user_id, result = await _create(db, monkeypatch)
        row = await _row(db)

    token = result['token']
    assert len(token) == 32
    assert re.fullmatch(r'[A-Za-z0-9_-]{32}', token)
    assert result['local_payment_id'] == row.id
    assert result['amount_kopeks'] == 10000
    assert result['amount_usd'] == '1.05'
    assert token in result['app_link']
    assert result['app_link'] == f'https://t.me/antarctic_wallet_bot/app?startapp=app_{APP_ID}_{token}'
    assert result['pay_page_url'] == f'https://bot.example.com/antarctic/pay/{token}'
    assert result['referral_url'] == 'https://t.me/antarctic_wallet_bot/app?startapp=ref_1'

    assert row.user_id == user_id
    assert row.token == token
    assert row.amount_kopeks == 10000
    assert row.amount_usd_cents == 105
    assert row.usd_rub_rate == Decimal('95.5000')
    assert (row.status, row.is_paid, row.operation_id, row.user_app_login) == ('pending', False, None, None)
    expected_expiry = before + timedelta(minutes=30)
    assert abs((row.expires_at - expected_expiry).total_seconds()) < 5
    assert result['expires_at'] == row.expires_at.isoformat()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'template',
    ['https://t.me/bot/app?startapp={unknown}_{token}', 'https://t.me/bot/app?startapp={0}', 'https://t.me/{app_id'],
    ids=['unknown-field', 'positional-field', 'unbalanced-brace'],
)
async def test_create_with_malformed_link_template_leaves_no_row(monkeypatch, template):
    """Ссылка собирается до записи в БД: опечатка в шаблоне не оставляет висящий pending-платёж."""
    _enable(monkeypatch, ANTARCTIC_APP_LINK_TEMPLATE=template)
    _install(monkeypatch, StubAW())
    async with memory_session(monkeypatch, TABLES) as db:
        user_id = await _seed_user(db)

        assert await _service().create_antarctic_payment(db, user_id=user_id, amount_kopeks=10000) is None

        await db.rollback()
        assert (await db.execute(select(func.count()).select_from(AntarcticPayment))).scalar_one() == 0


@pytest.mark.asyncio
async def test_create_applies_markup(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        _user_id, result = await _create(db, monkeypatch, ANTARCTIC_MARKUP_PERCENT=5.0)
        assert result['amount_usd'] == '1.10'
        assert (await _row(db)).amount_usd_cents == 110


# --- сессия мини-приложения --------------------------------------------------------


@pytest.mark.asyncio
async def test_session_binds_login_and_creates_intent(monkeypatch):
    stub = StubAW()
    async with memory_session(monkeypatch, TABLES) as db:
        _user_id, result = await _create(db, monkeypatch, stub)
        session = await _open(_service(), db, result['token'], 'login-1')
        row = await _row(db)

    assert stub.calls == [{'user_app_login': 'login-1', 'reference_id': result['token'], 'amount_usd': '1.05'}]
    assert session == {
        'operation_id': 'op-1',
        'amount_usd': '1.05',
        'amount_kopeks': 10000,
        'status': 'pending',
        'is_paid': False,
    }
    assert row.operation_id == 'op-1'
    assert row.user_app_login == 'login-1'
    assert row.aw_status == 'pending'
    assert row.status == 'pending'


@pytest.mark.asyncio
async def test_session_second_login_conflict(monkeypatch):
    stub = StubAW()
    async with memory_session(monkeypatch, TABLES) as db:
        _user_id, result = await _create(db, monkeypatch, stub)
        service = _service()
        await _open(service, db, result['token'], 'login-1')

        with pytest.raises(AntarcticSessionError) as exc:
            await _open(service, db, result['token'], 'login-2')
        row = await _row(db)

    assert exc.value.code == 'login_mismatch'
    assert exc.value.http_status == 409
    assert len(stub.calls) == 1
    assert row.user_app_login == 'login-1'


@pytest.mark.asyncio
async def test_session_repeat_same_login_is_idempotent(monkeypatch):
    stub = StubAW()
    async with memory_session(monkeypatch, TABLES) as db:
        _user_id, result = await _create(db, monkeypatch, stub)
        service = _service()
        first = await _open(service, db, result['token'])
        second = await _open(service, db, result['token'])
        count = (await db.execute(select(func.count()).select_from(AntarcticPayment))).scalar_one()
        row = await _row(db)

    assert first['operation_id'] == second['operation_id'] == 'op-1'
    assert count == 1
    assert row.operation_id == 'op-1'
    # повторное открытие страницы идёт тем же upsert — AW вернёт существующий интент
    assert [call['reference_id'] for call in stub.calls] == [result['token']] * 2


@pytest.mark.asyncio
async def test_session_expired_before_open(monkeypatch):
    stub = StubAW()
    async with memory_session(monkeypatch, TABLES) as db:
        _user_id, result = await _create(db, monkeypatch, stub)
        await _set_expires(db, datetime.now(UTC) - timedelta(minutes=1))

        with pytest.raises(AntarcticSessionError) as exc:
            await _open(_service(), db, result['token'])
        row = await _row(db)

    assert exc.value.code == 'expired'
    assert exc.value.http_status == 410
    assert row.status == 'expired'
    assert row.operation_id is None
    assert row.user_app_login is None
    assert stub.calls == []


@pytest.mark.asyncio
async def test_session_after_ttl_still_works_when_intent_already_exists(monkeypatch):
    """TTL останавливает только новые сессии; кто уже открыл страницу, может дооплатить."""
    stub = StubAW()
    async with memory_session(monkeypatch, TABLES) as db:
        _user_id, result = await _create(db, monkeypatch, stub)
        service = _service()
        await _open(service, db, result['token'])
        await _set_expires(db, datetime.now(UTC) - timedelta(minutes=5))

        again = await _open(service, db, result['token'])

    assert again['operation_id'] == 'op-1'
    assert again['status'] == 'pending'


@pytest.mark.asyncio
async def test_session_invalid_token(monkeypatch):
    stub = StubAW()
    async with memory_session(monkeypatch, TABLES) as db:
        _user_id, result = await _create(db, monkeypatch, stub)
        with pytest.raises(AntarcticSessionError) as exc:
            await _open(_service(), db, result['token'], 'bad')
        row = await _row(db)

    assert exc.value.code == 'invalid_token'
    assert exc.value.http_status == 401
    assert stub.calls == []
    assert row.user_app_login is None


@pytest.mark.asyncio
async def test_session_unknown_token_is_not_found(monkeypatch):
    stub = StubAW()
    async with memory_session(monkeypatch, TABLES) as db:
        await _create(db, monkeypatch, stub)
        with pytest.raises(AntarcticSessionError) as exc:
            await _open(_service(), db, 'no-such-token')
        in_transaction = db.in_transaction()

    assert not in_transaction
    assert exc.value.code == 'not_found'
    assert exc.value.http_status == 404
    assert stub.calls == []


@pytest.mark.asyncio
async def test_session_provider_error(monkeypatch):
    stub = StubAW()
    stub.error = AntarcticAPIError('boom', status_code=500)
    async with memory_session(monkeypatch, TABLES) as db:
        _user_id, result = await _create(db, monkeypatch, stub)
        with pytest.raises(AntarcticSessionError) as exc:
            await _open(_service(), db, result['token'])
        row = await _row(db)
        after_error = (row.operation_id, row.user_app_login, row.status)

        # после сбоя AW то же открытие страницы работает: привязка не «залипла»
        stub.error = None
        session = await _open(_service(), db, result['token'], 'login-2')

    assert exc.value.code == 'provider_error'
    assert exc.value.http_status == 502
    assert after_error == (None, None, 'pending')
    assert session['operation_id'] == 'op-1'


@pytest.mark.asyncio
async def test_session_insufficient_balance_reports_amount(monkeypatch):
    stub = StubAW()
    stub.error = AntarcticAPIError('INSUFFICIENT_BALANCE', status_code=422, code='INSUFFICIENT_BALANCE')
    async with memory_session(monkeypatch, TABLES) as db:
        _user_id, result = await _create(db, monkeypatch, stub)
        with pytest.raises(AntarcticSessionError) as exc:
            await _open(_service(), db, result['token'])
        row = await _row(db)
        after_error = (row.operation_id, row.user_app_login, row.status)

    assert exc.value.code == 'insufficient_balance'
    assert exc.value.http_status == 422
    assert exc.value.amount_usd == result['amount_usd']
    assert after_error == (None, None, 'pending')


@pytest.mark.asyncio
async def test_session_without_client_is_provider_error(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        _user_id, result = await _create(db, monkeypatch)
        _install(monkeypatch, None)
        with pytest.raises(AntarcticSessionError) as exc:
            await _open(_service(), db, result['token'])

    assert exc.value.code == 'provider_error'


@pytest.mark.asyncio
async def test_session_with_different_operation_id_fails_closed(monkeypatch):
    """AW вернул другую операцию на тот же reference_id: не переключаемся молча."""
    stub = StubAW()
    async with memory_session(monkeypatch, TABLES) as db:
        _user_id, result = await _create(db, monkeypatch, stub)
        service = _service()
        await _open(service, db, result['token'])

        stub.operation_id = 'op-2'
        with pytest.raises(AntarcticSessionError) as exc:
            await _open(service, db, result['token'])
        row = await _row(db)

    assert exc.value.code == 'provider_error'
    assert row.operation_id == 'op-1'


@pytest.mark.asyncio
async def test_session_refuses_values_that_do_not_fit_the_columns(monkeypatch):
    """Длина колонок на SQLite не проверяется, а PostgreSQL ответит DataError — отсекаем заранее."""
    stub = StubAW()
    async with memory_session(monkeypatch, TABLES) as db:
        _user_id, result = await _create(db, monkeypatch, stub)
        service = _service()

        with pytest.raises(AntarcticSessionError) as too_long_login:
            await _open(service, db, result['token'], 'l' * 65)
        assert too_long_login.value.code == 'provider_error'
        assert stub.calls == []

        stub.operation_id = 'o' * 65
        with pytest.raises(AntarcticSessionError) as too_long_operation:
            await _open(service, db, result['token'], 'l' * 64)
        assert too_long_operation.value.code == 'provider_error'
        row = await _row(db)

    assert (row.operation_id, row.user_app_login) == (None, None)


@pytest.mark.asyncio
async def test_unchanged_pending_status_does_not_rewrite_the_row(monkeypatch):
    """Фоновая сверка каждый цикл получает тот же pending — строка не перезаписывается."""
    stub = StubAW()
    async with memory_session(monkeypatch, TABLES) as db:
        _user_id, result = await _create(db, monkeypatch, stub)
        service = _service()
        await _open(service, db, result['token'])
        before = await _row(db)
        updated_before, payload_before = before.updated_at, before.callback_payload

        for _ in range(3):
            refreshed = await service.refresh_antarctic_payment(db, result['local_payment_id'])
            assert refreshed['status'] == 'pending'
        after = await _row(db)

    assert after.updated_at == updated_before
    assert after.callback_payload == payload_before


@pytest.mark.asyncio
async def test_session_on_paid_payment_returns_paid_without_api_call(monkeypatch):
    stub = StubAW()
    async with memory_session(monkeypatch, TABLES) as db:
        _user_id, result = await _create(db, monkeypatch, stub)
        service = _service()
        await _open(service, db, result['token'])
        await service.process_antarctic_webhook(db, _event(result['token']))
        calls_before = len(stub.calls)

        session = await _open(service, db, result['token'], 'login-2')

    assert session['is_paid'] is True
    assert session['status'] == 'success'
    assert len(stub.calls) == calls_before


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['rejected', 'failed', 'expired'])
async def test_session_on_terminal_payment_is_expired(monkeypatch, status):
    stub = StubAW()
    async with memory_session(monkeypatch, TABLES) as db:
        _user_id, result = await _create(db, monkeypatch, stub)
        service = _service()
        await _open(service, db, result['token'])
        await service.process_antarctic_webhook(db, _event(result['token'], event='intent.rejected', status=status))
        calls_before = len(stub.calls)

        with pytest.raises(AntarcticSessionError) as exc:
            await _open(service, db, result['token'])

    assert exc.value.code == 'expired'
    assert len(stub.calls) == calls_before


@pytest.mark.asyncio
async def test_session_credits_when_intent_is_already_approved(monkeypatch):
    stub = StubAW(status='approved')
    async with memory_session(monkeypatch, TABLES) as db:
        user_id, result = await _create(db, monkeypatch, stub)
        session = await _open(_service(), db, result['token'])

        assert session['is_paid'] is True
        assert session['status'] == 'success'
        assert await _balance(db, user_id) == 10000
        assert await _deposits(db) == 1


@pytest.mark.asyncio
async def test_session_with_unknown_status_changes_nothing(monkeypatch):
    stub = StubAW(status='unknown')
    async with memory_session(monkeypatch, TABLES) as db:
        _user_id, result = await _create(db, monkeypatch, stub)
        session = await _open(_service(), db, result['token'])
        row = await _row(db)

    assert session['status'] == 'pending'
    assert session['is_paid'] is False
    assert row.status == 'pending'
    assert row.operation_id == 'op-1'


# --- вебхук и зачисление -----------------------------------------------------------


@pytest.mark.asyncio
async def test_webhook_approved_credits_once(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        user_id, result = await _create(db, monkeypatch)
        service = _service()
        await _open(service, db, result['token'])

        assert await service.process_antarctic_webhook(db, _event(result['token'])) is True
        assert await _balance(db, user_id) == 10000

        # at-least-once: тот же вебхук пришёл повторно — второй раз не зачисляем
        assert await service.process_antarctic_webhook(db, _event(result['token'])) is True
        assert await _balance(db, user_id) == 10000

        assert await _deposits(db) == 1
        transaction = (await db.execute(select(Transaction))).scalar_one()
        row = await _row(db)

    assert transaction.payment_method == 'antarctic'
    assert transaction.external_id == result['token']
    assert transaction.type == 'deposit'
    assert transaction.amount_kopeks == 10000
    assert transaction.user_id == user_id
    assert transaction.description == f'Пополнение через {settings.get_antarctic_display_name()} ($1.05)'
    assert row.is_paid is True
    assert row.status == 'success'
    assert row.aw_status == 'approved'
    assert row.paid_at is not None
    assert row.transaction_id == transaction.id
    assert row.callback_payload['source'] == 'webhook'
    assert row.metadata_json['balance_credited'] is True


@pytest.mark.asyncio
async def test_webhook_then_refresh_no_double_credit(monkeypatch):
    stub = StubAW()
    async with memory_session(monkeypatch, TABLES) as db:
        user_id, result = await _create(db, monkeypatch, stub)
        service = _service()
        await _open(service, db, result['token'])
        assert await service.process_antarctic_webhook(db, _event(result['token'])) is True

        stub.status = 'approved'
        refreshed = await service.refresh_antarctic_payment(db, result['local_payment_id'])

        assert refreshed['is_paid'] is True
        assert refreshed['status'] == 'success'
        assert await _balance(db, user_id) == 10000
        assert await _deposits(db) == 1


@pytest.mark.asyncio
async def test_refresh_then_webhook_no_double_credit(monkeypatch):
    stub = StubAW()
    async with memory_session(monkeypatch, TABLES) as db:
        user_id, result = await _create(db, monkeypatch, stub)
        service = _service()
        await _open(service, db, result['token'])

        stub.status = 'approved'
        refreshed = await service.refresh_antarctic_payment(db, result['local_payment_id'])
        assert refreshed['is_paid'] is True

        assert await service.process_antarctic_webhook(db, _event(result['token'])) is True
        assert await _balance(db, user_id) == 10000
        assert await _deposits(db) == 1


@pytest.mark.asyncio
async def test_webhook_with_stale_row_does_not_double_credit(monkeypatch):
    """Второй обработчик держит копию строки из времени «pending» — после блокировки он видит оплату."""
    async with memory_session(monkeypatch, TABLES) as db:
        user_id, result = await _create(db, monkeypatch)
        service = _service()
        await _open(service, db, result['token'])
        stale = await _row(db)

        assert await service.process_antarctic_webhook(db, _event(result['token'])) is True
        assert await _balance(db, user_id) == 10000

        # Возвращаем копию в память в состоянии «ожидает оплаты», БД не трогаем.
        set_committed_value(stale, 'is_paid', False)
        set_committed_value(stale, 'status', 'pending')
        set_committed_value(stale, 'transaction_id', None)
        set_committed_value(stale, 'metadata_json', {})

        assert await service.process_antarctic_webhook(db, _event(result['token'])) is True
        assert await _balance(db, user_id) == 10000
        assert await _deposits(db) == 1


@pytest.mark.asyncio
async def test_apply_twice_in_a_row_credits_once(monkeypatch):
    """Повторное применение approved к уже зачисленной строке (вызов под блокировкой) — без второго зачисления."""
    async with memory_session(monkeypatch, TABLES) as db:
        user_id, result = await _create(db, monkeypatch)
        service = _service()
        await _open(service, db, result['token'])

        for _ in range(2):
            payment = await _row(db)
            assert (
                await service._apply_antarctic_intent(
                    db,
                    payment,
                    status='approved',
                    operation_id='op-1',
                    amount='1.05',
                    source='test',
                    raw={},
                )
                is True
            )
            await db.commit()

        assert await _balance(db, user_id) == 10000
        assert await _deposits(db) == 1


@pytest.mark.asyncio
async def test_finalize_is_idempotent_for_a_linked_payment(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        user_id, result = await _create(db, monkeypatch)
        service = _service()
        await _open(service, db, result['token'])
        await service.process_antarctic_webhook(db, _event(result['token']))

        payment = await _row(db)
        assert await service._finalize_antarctic_payment(db, payment, trigger='test') is True
        assert await _balance(db, user_id) == 10000
        assert await _deposits(db) == 1


@pytest.mark.asyncio
async def test_existing_deposit_for_token_is_never_credited_again(monkeypatch):
    """Депозит с этим external_id уже есть, а платёж к нему не привязан: баланс второй раз не трогаем."""
    async with memory_session(monkeypatch, TABLES) as db:
        user_id, result = await _create(db, monkeypatch)
        service = _service()
        await _open(service, db, result['token'])
        db.add(
            Transaction(
                user_id=user_id,
                type='deposit',
                amount_kopeks=10000,
                description='earlier',
                payment_method=PaymentMethod.ANTARCTIC.value,
                external_id=result['token'],
                is_completed=True,
            )
        )
        await db.commit()

        assert await service.process_antarctic_webhook(db, _event(result['token'])) is True
        transaction_id = (await _row(db)).transaction_id

        assert await _balance(db, user_id) == 0
        assert await _deposits(db) == 1
        assert transaction_id is not None


@pytest.mark.asyncio
async def test_approved_after_ttl_is_credited(monkeypatch):
    stub = StubAW()
    async with memory_session(monkeypatch, TABLES) as db:
        user_id, result = await _create(db, monkeypatch, stub)
        service = _service()
        await _open(service, db, result['token'])
        await _set_expires(db, datetime.now(UTC) - timedelta(hours=2))

        stub.status = 'approved'
        refreshed = await service.refresh_antarctic_payment(db, result['local_payment_id'])

        assert refreshed['is_paid'] is True
        assert await _balance(db, user_id) == 10000
        assert await _deposits(db) == 1


@pytest.mark.asyncio
async def test_webhook_approved_after_ttl_is_credited(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        user_id, result = await _create(db, monkeypatch)
        service = _service()
        await _open(service, db, result['token'])
        await _set_expires(db, datetime.now(UTC) - timedelta(hours=2))

        assert await service.process_antarctic_webhook(db, _event(result['token'])) is True
        assert await _balance(db, user_id) == 10000


@pytest.mark.asyncio
async def test_pending_after_ttl_is_not_expired_while_aw_intent_is_open(monkeypatch):
    """Локальный TTL не гасит платёж с живым интентом: позднее approved всё равно придёт."""
    stub = StubAW()
    async with memory_session(monkeypatch, TABLES) as db:
        user_id, result = await _create(db, monkeypatch, stub)
        service = _service()
        await _open(service, db, result['token'])
        await _set_expires(db, datetime.now(UTC) - timedelta(hours=2))

        refreshed = await service.refresh_antarctic_payment(db, result['local_payment_id'])
        assert refreshed['status'] == 'pending'

        assert await service.process_antarctic_webhook(db, _event(result['token'])) is True
        assert await _balance(db, user_id) == 10000


@pytest.mark.asyncio
@pytest.mark.parametrize('amount', ['1.04', '1.06', '0', 'abc', 'NaN', 'Infinity', '1.05.1'])
async def test_amount_mismatch_not_credited(monkeypatch, amount):
    async with memory_session(monkeypatch, TABLES) as db:
        user_id, result = await _create(db, monkeypatch)
        service = _service()
        await _open(service, db, result['token'])

        # Повтор не исправит расхождение — подтверждаем (2xx), платёж ждёт ручного разбора.
        assert await service.process_antarctic_webhook(db, _event(result['token'], amount=amount)) is True
        row = await _row(db)

        assert row.status == 'amount_mismatch'
        assert row.is_paid is False
        assert row.aw_status == 'approved'
        assert await _balance(db, user_id) == 0
        assert await _deposits(db) == 0

        # И поздний «правильный» вебхук уже не зачисляет платёж из финального статуса.
        assert await service.process_antarctic_webhook(db, _event(result['token'])) is True
        assert await _balance(db, user_id) == 0
        assert (await _row(db)).status == 'amount_mismatch'


@pytest.mark.asyncio
async def test_amount_given_as_number_or_with_trailing_zeros_matches(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        user_id, result = await _create(db, monkeypatch)
        service = _service()
        await _open(service, db, result['token'])

        assert await service.process_antarctic_webhook(db, _event(result['token'], amount='1.050')) is True
        assert await _balance(db, user_id) == 10000


@pytest.mark.asyncio
async def test_amount_mismatch_through_session_not_credited(monkeypatch):
    stub = StubAW(status='approved', amount='9.99')
    async with memory_session(monkeypatch, TABLES) as db:
        user_id, result = await _create(db, monkeypatch, stub)
        service = _service()
        # approved уже при открытии страницы, но сумма чужая
        session = await _open(service, db, result['token'])
        assert session['is_paid'] is False
        assert session['status'] == 'amount_mismatch'
        assert await _balance(db, user_id) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('missing', [None, '', '   '])
async def test_approved_without_amount_asks_for_retry(monkeypatch, missing):
    """Вебхук approved без суммы: сверяем с AW; пока интент там pending — не зачисляем и просим повтор."""
    stub = StubAW()
    async with memory_session(monkeypatch, TABLES) as db:
        user_id, result = await _create(db, monkeypatch, stub)
        service = _service()
        await _open(service, db, result['token'])

        assert await service.process_antarctic_webhook(db, _event(result['token'], amount=missing)) is False
        row = await _row(db)
        assert (row.status, row.is_paid) == ('pending', False)
        assert await _balance(db, user_id) == 0
        # сессия + сверка с AW; саму сумму вебхук не подставляет
        assert len(stub.calls) == 2

        # повтор доставки уже с суммой закрывает платёж
        assert await service.process_antarctic_webhook(db, _event(result['token'])) is True
        assert await _balance(db, user_id) == 10000


@pytest.mark.asyncio
@pytest.mark.parametrize('aw_amount', [_SAME, None], ids=['aw-echoes-amount', 'aw-omits-amount'])
async def test_webhook_without_amount_is_settled_through_the_api(monkeypatch, aw_amount):
    """approved без суммы в вебхуке: платёж закрывает запрос к AW, вебхук отвечает 2xx."""
    stub = StubAW(amount=aw_amount)
    async with memory_session(monkeypatch, TABLES) as db:
        user_id, result = await _create(db, monkeypatch, stub)
        service = _service()
        await _open(service, db, result['token'])

        stub.status = 'approved'
        assert await service.process_antarctic_webhook(db, _event(result['token'], amount=None)) is True
        row = await _row(db)
        assert (row.status, row.is_paid) == ('success', True)
        assert row.callback_payload['source'] == 'api_check'
        assert await _balance(db, user_id) == 10000
        assert await _deposits(db) == 1

        # повторная доставка: платёж закрыт, лишнего запроса к AW и второго зачисления нет
        calls_before = len(stub.calls)
        assert await service.process_antarctic_webhook(db, _event(result['token'], amount=None)) is True
        assert len(stub.calls) == calls_before
        assert await _balance(db, user_id) == 10000
        assert await _deposits(db) == 1


@pytest.mark.asyncio
async def test_webhook_without_amount_for_unopened_payment_asks_for_retry(monkeypatch):
    """Страницу никто не открывал: проверять нечем (логина нет), зачислять нельзя — AW повторит."""
    stub = StubAW(status='approved')
    async with memory_session(monkeypatch, TABLES) as db:
        user_id, result = await _create(db, monkeypatch, stub)

        assert await _service().process_antarctic_webhook(db, _event(result['token'], amount=None)) is False
        assert stub.calls == []
        assert (await _row(db)).is_paid is False
        assert await _balance(db, user_id) == 0


@pytest.mark.asyncio
async def test_amountless_webhook_for_unbound_row_past_ttl_is_not_expired(monkeypatch):
    """AW уже говорит «оплачено», а у нас нет логина кошелька: гасить строку по TTL нельзя.

    Сверка через API здесь выглядела бы как «страницу не открывали» и закрыла бы платёж как
    expired, а вебхук ответил бы 2xx — деньги ушли бы без следа. Вместо этого: тревога и 5xx,
    чтобы AW повторил доставку.
    """
    alerts: list[dict[str, Any]] = []
    monkeypatch.setattr(aw_module.alert_logger, 'error', lambda *_args, **kwargs: alerts.append(kwargs))
    stub = StubAW(status='approved')
    async with memory_session(monkeypatch, TABLES) as db:
        user_id, result = await _create(db, monkeypatch, stub)
        await _set_expires(db, datetime.now(UTC) - timedelta(minutes=5))

        assert await _service().process_antarctic_webhook(db, _event(result['token'], amount=None)) is False

        row = await _row(db)
        assert (row.status, row.is_paid) == ('pending', False)
        assert stub.calls == []
        assert await _balance(db, user_id) == 0

    assert len(alerts) == 1
    assert alerts[0]['payment_id'] == result['local_payment_id']
    assert alerts[0]['source'] == 'webhook'


@pytest.mark.asyncio
@pytest.mark.parametrize('how', ['rejected', 'expired_unopened'])
async def test_amountless_webhook_on_a_terminal_payment_raises_an_alert(monkeypatch, how):
    """approved без суммы по платежу, уже закрытому как неуспешный: деньги могли уйти — зовём человека."""
    alerts: list[dict[str, Any]] = []
    monkeypatch.setattr(aw_module.alert_logger, 'error', lambda *_args, **kwargs: alerts.append(kwargs))
    stub = StubAW()
    async with memory_session(monkeypatch, TABLES) as db:
        user_id, result = await _create(db, monkeypatch, stub)
        service = _service()
        if how == 'rejected':
            await _open(service, db, result['token'])
            rejected = _event(result['token'], event='intent.rejected', status='rejected', amount=None)
            assert await service.process_antarctic_webhook(db, rejected) is True
            expected_status = 'rejected'
        else:
            await _set_expires(db, datetime.now(UTC) - timedelta(minutes=5))
            await service.refresh_antarctic_payment(db, result['local_payment_id'])
            expected_status = 'expired'
        assert (await _row(db)).status == expected_status
        assert alerts == []  # закрытие без approved — не повод для тревоги
        calls_before = len(stub.calls)

        assert await service.process_antarctic_webhook(db, _event(result['token'], amount=None)) is True

        assert len(stub.calls) == calls_before
        row = await _row(db)
        assert (row.status, row.is_paid) == (expected_status, False)
        assert await _balance(db, user_id) == 0

    assert len(alerts) == 1
    assert alerts[0]['current_status'] == expected_status
    assert alerts[0]['source'] == 'webhook'


@pytest.mark.asyncio
async def test_amountless_webhook_closing_as_amount_mismatch_alerts_once(monkeypatch):
    """Сверка через API нашла чужую сумму: тревога уже поднята при применении статуса, дубля нет."""
    alerts: list[dict[str, Any]] = []
    monkeypatch.setattr(aw_module.alert_logger, 'error', lambda *_args, **kwargs: alerts.append(kwargs))
    stub = StubAW()
    async with memory_session(monkeypatch, TABLES) as db:
        user_id, result = await _create(db, monkeypatch, stub)
        service = _service()
        await _open(service, db, result['token'])
        stub.status = 'approved'
        stub.amount = '0.01'

        assert await service.process_antarctic_webhook(db, _event(result['token'], amount=None)) is True

        assert (await _row(db)).status == 'amount_mismatch'
        assert await _balance(db, user_id) == 0

    assert len(alerts) == 1


@pytest.mark.asyncio
async def test_webhook_without_amount_on_a_closed_payment_is_acknowledged(monkeypatch):
    stub = StubAW()
    async with memory_session(monkeypatch, TABLES) as db:
        user_id, result = await _create(db, monkeypatch, stub)
        service = _service()
        await _open(service, db, result['token'])
        rejected = _event(result['token'], event='intent.rejected', status='rejected', amount=None)
        assert await service.process_antarctic_webhook(db, rejected) is True
        calls_before = len(stub.calls)

        assert await service.process_antarctic_webhook(db, _event(result['token'], amount=None)) is True
        assert len(stub.calls) == calls_before
        assert await _balance(db, user_id) == 0


@pytest.mark.asyncio
async def test_webhook_without_amount_for_another_operation_is_ignored(monkeypatch):
    alerts: list[dict[str, Any]] = []
    monkeypatch.setattr(aw_module.alert_logger, 'error', lambda *_args, **kwargs: alerts.append(kwargs))
    stub = StubAW()
    async with memory_session(monkeypatch, TABLES) as db:
        user_id, result = await _create(db, monkeypatch, stub)
        service = _service()
        await _open(service, db, result['token'])
        stub.status = 'approved'
        calls_before = len(stub.calls)

        foreign = _event(result['token'], amount=None, operation_id='op-OTHER')
        assert await service.process_antarctic_webhook(db, foreign) is True
        assert len(stub.calls) == calls_before
        assert (await _row(db)).is_paid is False
        assert await _balance(db, user_id) == 0

    assert alerts and alerts[-1]['received_operation_id'] == 'op-OTHER'


@pytest.mark.asyncio
async def test_refresh_credits_when_aw_omits_the_amount(monkeypatch):
    """Наш собственный upsert: если AW не вернул data.amount, сверяем с суммой запроса."""
    stub = StubAW(amount=None)
    async with memory_session(monkeypatch, TABLES) as db:
        user_id, result = await _create(db, monkeypatch, stub)
        service = _service()
        await _open(service, db, result['token'])

        stub.status = 'approved'
        refreshed = await service.refresh_antarctic_payment(db, result['local_payment_id'])
        assert (refreshed['status'], refreshed['is_paid']) == ('success', True)
        assert await _balance(db, user_id) == 10000
        assert await _deposits(db) == 1

        again = await service.refresh_antarctic_payment(db, result['local_payment_id'])
        assert again['is_paid'] is True
        assert await _balance(db, user_id) == 10000
        assert await _deposits(db) == 1


@pytest.mark.asyncio
async def test_session_credits_when_aw_omits_the_amount(monkeypatch):
    stub = StubAW(status='approved', amount=None)
    async with memory_session(monkeypatch, TABLES) as db:
        user_id, result = await _create(db, monkeypatch, stub)
        session = await _open(_service(), db, result['token'])

        assert (session['status'], session['is_paid']) == ('success', True)
        assert await _balance(db, user_id) == 10000
        assert await _deposits(db) == 1


@pytest.mark.asyncio
async def test_apply_without_amount_never_credits(monkeypatch):
    """Страховка прямого вызова: approved без суммы не зачисляется и ничего не меняет."""
    async with memory_session(monkeypatch, TABLES) as db:
        user_id, result = await _create(db, monkeypatch)
        service = _service()
        await _open(service, db, result['token'])
        payment = await _row(db)

        assert (
            await service._apply_antarctic_intent(
                db, payment, status='approved', operation_id='op-1', amount=None, source='webhook', raw={}
            )
            is False
        )
        await db.rollback()
        assert (await _row(db)).is_paid is False
        assert await _balance(db, user_id) == 0


@pytest.mark.asyncio
async def test_rejected_then_approved_ignored(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        user_id, result = await _create(db, monkeypatch)
        service = _service()
        await _open(service, db, result['token'])

        rejected = _event(result['token'], event='intent.rejected', status='rejected', amount=None)
        assert await service.process_antarctic_webhook(db, rejected) is True
        assert (await _row(db)).status == 'rejected'

        assert await service.process_antarctic_webhook(db, _event(result['token'])) is True
        row = await _row(db)
        assert row.status == 'rejected'
        assert row.is_paid is False
        assert await _balance(db, user_id) == 0
        assert await _deposits(db) == 0


@pytest.mark.asyncio
async def test_approved_on_a_terminal_payment_raises_an_alert(monkeypatch):
    """AW говорит «оплачено», а у нас платёж закрыт как неуспешный: деньги могли уйти — зовём человека."""
    alerts: list[dict[str, Any]] = []
    monkeypatch.setattr(aw_module.alert_logger, 'error', lambda *_args, **kwargs: alerts.append(kwargs))
    async with memory_session(monkeypatch, TABLES) as db:
        user_id, result = await _create(db, monkeypatch)
        service = _service()
        await _open(service, db, result['token'])

        rejected = _event(result['token'], event='intent.rejected', status='rejected', amount=None)
        assert await service.process_antarctic_webhook(db, rejected) is True
        assert alerts == []  # повторный неуспех — не повод для тревоги

        assert await service.process_antarctic_webhook(db, _event(result['token'])) is True
        assert await _balance(db, user_id) == 0

    assert len(alerts) == 1
    assert alerts[0]['current_status'] == 'rejected'
    assert alerts[0]['source'] == 'webhook'


@pytest.mark.asyncio
async def test_rejected_through_refresh_stops_the_payment(monkeypatch):
    stub = StubAW()
    async with memory_session(monkeypatch, TABLES) as db:
        _user_id, result = await _create(db, monkeypatch, stub)
        service = _service()
        await _open(service, db, result['token'])

        stub.status = 'rejected'
        refreshed = await service.refresh_antarctic_payment(db, result['local_payment_id'])

    assert refreshed['status'] == 'rejected'
    assert refreshed['is_paid'] is False


@pytest.mark.asyncio
async def test_operation_id_mismatch_ignored(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        user_id, result = await _create(db, monkeypatch)
        service = _service()
        await _open(service, db, result['token'])

        foreign = _event(result['token'], operation_id='op-OTHER')
        assert await service.process_antarctic_webhook(db, foreign) is True
        row = await _row(db)

        assert (row.status, row.is_paid, row.operation_id) == ('pending', False, 'op-1')
        assert await _balance(db, user_id) == 0
        assert await _deposits(db) == 0


@pytest.mark.asyncio
async def test_refresh_with_another_operation_alerts_once_and_stops_polling(monkeypatch):
    """AW отвечает на наш reference_id чужой операцией: тревога один раз, дальше фоновая сверка молчит."""
    alerts: list[dict[str, Any]] = []
    monkeypatch.setattr(aw_module.alert_logger, 'error', lambda *_args, **kwargs: alerts.append(kwargs))
    stub = StubAW()
    async with memory_session(monkeypatch, TABLES) as db:
        user_id, result = await _create(db, monkeypatch, stub)
        service = _service()
        await _open(service, db, result['token'])
        stub.operation_id = 'op-2'

        first = await service.refresh_antarctic_payment(db, result['local_payment_id'])
        assert (first['status'], first['is_paid']) == ('pending', False)
        assert len(alerts) == 1
        assert alerts[0]['received_operation_id'] == 'op-2'
        calls_after_first = len(stub.calls)

        row = await _row(db)
        assert row.operation_id == 'op-1'
        assert row.metadata_json['operation_id_mismatch']['received_operation_id'] == 'op-2'
        assert row.metadata_json['user_id'] == user_id  # остальные метаданные на месте

        for _ in range(3):
            again = await service.refresh_antarctic_payment(db, result['local_payment_id'])
            assert (again['status'], again['is_paid']) == ('pending', False)

        assert len(alerts) == 1
        assert len(stub.calls) == calls_after_first
        assert await _balance(db, user_id) == 0


@pytest.mark.asyncio
async def test_foreign_operation_webhook_does_not_stop_polling(monkeypatch):
    """Вебхук по чужой операции может быть запоздалой доставкой: сверку нашей операции он не отключает."""
    alerts: list[dict[str, Any]] = []
    monkeypatch.setattr(aw_module.alert_logger, 'error', lambda *_args, **kwargs: alerts.append(kwargs))
    stub = StubAW()
    async with memory_session(monkeypatch, TABLES) as db:
        user_id, result = await _create(db, monkeypatch, stub)
        service = _service()
        await _open(service, db, result['token'])

        foreign = _event(result['token'], operation_id='op-OTHER')
        assert await service.process_antarctic_webhook(db, foreign) is True
        assert 'operation_id_mismatch' not in ((await _row(db)).metadata_json or {})

        calls_before = len(stub.calls)
        stub.status = 'approved'
        refreshed = await service.refresh_antarctic_payment(db, result['local_payment_id'])
        assert len(stub.calls) == calls_before + 1
        assert refreshed['is_paid'] is True
        assert await _balance(db, user_id) == 10000


@pytest.mark.asyncio
async def test_webhook_without_operation_id_on_open_payment_adopts_it(monkeypatch):
    """Строка не успела сохранить operation_id (сбой после ответа AW): подписанный вебхук всё равно зачисляет."""
    async with memory_session(monkeypatch, TABLES) as db:
        user_id, result = await _create(db, monkeypatch)
        service = _service()

        assert await service.process_antarctic_webhook(db, _event(result['token'], operation_id='op-9')) is True
        row = await _row(db)

        assert row.operation_id == 'op-9'
        assert row.is_paid is True
        assert await _balance(db, user_id) == 10000


@pytest.mark.asyncio
async def test_webhook_found_by_operation_id_when_reference_is_missing(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        user_id, result = await _create(db, monkeypatch)
        service = _service()
        await _open(service, db, result['token'])

        assert await service.process_antarctic_webhook(db, _event(None)) is True
        assert await _balance(db, user_id) == 10000


@pytest.mark.asyncio
async def test_webhook_status_falls_back_to_event_name(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        user_id, result = await _create(db, monkeypatch)
        service = _service()
        await _open(service, db, result['token'])

        assert await service.process_antarctic_webhook(db, _event(result['token'], status=None)) is True
        assert await _balance(db, user_id) == 10000


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'payload',
    [
        {'event': 'app.status_changed', 'data': {'type': 'pay'}},
        {'event': 'intent.approved', 'data': {'type': 'receive', 'reference_id': 'x', 'status': 'approved'}},
        {'event': 'intent.approved', 'data': 'not-a-dict'},
        {'event': 'intent.approved'},
        {'event': None},
        {},
    ],
)
async def test_unknown_event_is_acknowledged_without_changes(monkeypatch, payload):
    async with memory_session(monkeypatch, TABLES) as db:
        user_id, result = await _create(db, monkeypatch)
        service = _service()
        await _open(service, db, result['token'])

        assert await service.process_antarctic_webhook(db, payload) is True
        row = await _row(db)

        assert (row.status, row.is_paid) == ('pending', False)
        assert await _balance(db, user_id) == 0


@pytest.mark.asyncio
async def test_unknown_reference_is_acknowledged(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        user_id, result = await _create(db, monkeypatch)
        service = _service()
        await _open(service, db, result['token'])

        foreign = _event('not-our-token', operation_id='op-foreign')
        assert await service.process_antarctic_webhook(db, foreign) is True
        assert await _balance(db, user_id) == 0
        assert (await _row(db)).is_paid is False


@pytest.mark.asyncio
async def test_webhook_error_asks_for_retry_and_leaves_no_partial_state(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        user_id, result = await _create(db, monkeypatch)
        service = _service()
        await _open(service, db, result['token'])

        async def boom(*_args: Any, **_kwargs: Any) -> bool:
            raise RuntimeError('db went away')

        service._finalize_antarctic_payment = boom  # type: ignore[method-assign]
        assert await service.process_antarctic_webhook(db, _event(result['token'])) is False

        row = await _row(db)
        assert (row.status, row.is_paid, row.transaction_id) == ('pending', False, None)
        assert await _balance(db, user_id) == 0

        # AW повторит доставку — теперь всё проходит
        del service._finalize_antarctic_payment
        assert await service.process_antarctic_webhook(db, _event(result['token'])) is True
        assert await _balance(db, user_id) == 10000
        assert await _deposits(db) == 1


@pytest.mark.asyncio
async def test_missing_user_is_not_marked_paid_and_retry_works(monkeypatch):
    """Пользователя нет — зачислить нельзя; оплаченным платёж не помечаем, повтор потом сработает."""
    async with memory_session(monkeypatch, TABLES) as db:
        user_id, result = await _create(db, monkeypatch)
        service = _service()
        await _open(service, db, result['token'])
        await db.execute(delete(User).where(User.id == user_id))
        await db.commit()
        db.expunge_all()

        assert await service.process_antarctic_webhook(db, _event(result['token'])) is False
        row = await _row(db)
        assert (row.status, row.is_paid) == ('pending', False)
        assert await _deposits(db) == 0

        db.add(User(id=user_id, telegram_id=555, username='payer', language='ru', balance_kopeks=0))
        await db.commit()
        assert await service.process_antarctic_webhook(db, _event(result['token'])) is True
        assert await _balance(db, user_id) == 10000


@pytest.mark.asyncio
async def test_first_topup_flag_and_notifications(monkeypatch):
    sent: list[tuple[int, str]] = []

    class FakeBot:
        async def send_message(self, chat_id, text, **_kwargs):
            sent.append((chat_id, text))

    async with memory_session(monkeypatch, TABLES) as db:
        user_id, result = await _create(db, monkeypatch)
        service = _service()
        service.bot = FakeBot()
        await _open(service, db, result['token'])

        assert await service.process_antarctic_webhook(db, _event(result['token'])) is True
        user = (
            await db.execute(select(User).where(User.id == user_id).execution_options(populate_existing=True))
        ).scalar_one()

        assert user.balance_kopeks == 10000
        assert user.has_made_first_topup is True

    user_messages = [text for chat_id, text in sent if chat_id == 555]
    assert len(user_messages) == 1
    assert 'Пополнение успешно' in user_messages[0]


# --- сверка через API --------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_unknown_payment_returns_none(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        await _create(db, monkeypatch)
        assert await _service().refresh_antarctic_payment(db, 424242) is None
        assert await _service().get_antarctic_payment_status(db, 424242) is None


@pytest.mark.asyncio
async def test_refresh_before_anyone_opened_the_page_changes_nothing(monkeypatch):
    stub = StubAW(status='approved')
    async with memory_session(monkeypatch, TABLES) as db:
        _user_id, result = await _create(db, monkeypatch, stub)
        refreshed = await _service().refresh_antarctic_payment(db, result['local_payment_id'])

    assert refreshed['status'] == 'pending'
    assert refreshed['is_paid'] is False
    # логина ещё нет — слать в AW нечего
    assert stub.calls == []


@pytest.mark.asyncio
async def test_refresh_expires_a_payment_nobody_opened(monkeypatch):
    stub = StubAW(status='approved')
    async with memory_session(monkeypatch, TABLES) as db:
        _user_id, result = await _create(db, monkeypatch, stub)
        await _set_expires(db, datetime.now(UTC) - timedelta(minutes=1))

        refreshed = await _service().refresh_antarctic_payment(db, result['local_payment_id'])
        row = await _row(db)

    assert refreshed['status'] == 'expired'
    assert row.status == 'expired'
    assert stub.calls == []


@pytest.mark.asyncio
async def test_refresh_provider_error_keeps_state(monkeypatch):
    stub = StubAW()
    async with memory_session(monkeypatch, TABLES) as db:
        _user_id, result = await _create(db, monkeypatch, stub)
        service = _service()
        await _open(service, db, result['token'])

        stub.error = AntarcticAPIError('down', status_code=503)
        refreshed = await service.refresh_antarctic_payment(db, result['local_payment_id'])

    assert refreshed['status'] == 'pending'
    assert refreshed['is_paid'] is False


@pytest.mark.asyncio
async def test_refresh_with_unknown_status_changes_nothing(monkeypatch):
    stub = StubAW()
    async with memory_session(monkeypatch, TABLES) as db:
        _user_id, result = await _create(db, monkeypatch, stub)
        service = _service()
        await _open(service, db, result['token'])

        stub.status = 'unknown'
        refreshed = await service.refresh_antarctic_payment(db, result['local_payment_id'])
        assert refreshed['status'] == 'pending'

        stub.status = 'some-new-aw-status'
        refreshed = await service.refresh_antarctic_payment(db, result['local_payment_id'])
        assert refreshed['status'] == 'pending'


@pytest.mark.asyncio
async def test_refresh_paid_and_terminal_payments_skip_the_api(monkeypatch):
    stub = StubAW()
    async with memory_session(monkeypatch, TABLES) as db:
        _user_id, result = await _create(db, monkeypatch, stub)
        service = _service()
        await _open(service, db, result['token'])
        await service.process_antarctic_webhook(db, _event(result['token']))
        calls_before = len(stub.calls)

        refreshed = await service.refresh_antarctic_payment(db, result['local_payment_id'])
        assert (refreshed['status'], refreshed['is_paid']) == ('success', True)
        assert len(stub.calls) == calls_before


@pytest.mark.asyncio
async def test_get_antarctic_payment_status_is_the_refresh_alias(monkeypatch):
    stub = StubAW()
    async with memory_session(monkeypatch, TABLES) as db:
        user_id, result = await _create(db, monkeypatch, stub)
        service = _service()
        await _open(service, db, result['token'])

        stub.status = 'approved'
        status = await service.get_antarctic_payment_status(db, result['local_payment_id'])

        assert status['is_paid'] is True
        assert status['status'] == 'success'
        assert await _balance(db, user_id) == 10000
