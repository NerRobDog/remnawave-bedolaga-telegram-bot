"""Antarctic Wallet: гонки денежного пути на настоящем PostgreSQL.

SQLite игнорирует ``FOR UPDATE``, поэтому «вебхук и фоновая сверка пришли одновременно» там
проверяется только для вида. Здесь у каждой стороны своя сессия и своё соединение, а
соперники ставятся в очередь за одну и ту же блокировку строки платежа: держатель блокировки
удерживает её, пока в ``pg_stat_activity`` не появятся оба ожидающих, и только потом отпускает.

Каждая проверка обязана падать, если из ``get_antarctic_payment_by_id_for_update`` убрать
``.with_for_update()`` или ``populate_existing=True``.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.antarctic import get_antarctic_payment_by_id_for_update
from app.database.models import AntarcticPayment, PaymentMethod, Transaction, User
from app.services.payment.antarctic import AntarcticSessionError
from tests.fixtures.postgres_db import postgres_sessions
from tests.services.test_payment_service_antarctic import StubAW, _enable, _event, _install, _service


pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]

TABLES = [User.__table__, Transaction.__table__, AntarcticPayment.__table__]


async def _waiters(watcher: AsyncSession) -> int:
    result = await watcher.execute(
        text("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND wait_event_type = 'Lock'")
    )
    count = result.scalar() or 0
    await watcher.rollback()  # наблюдатель не держит снимок
    return count


async def _wait_for_waiters(watcher: AsyncSession, expected: int, timeout: float = 5.0) -> int:
    """Ждёт, пока в очереди за блокировкой окажется ``expected`` сессий; возвращает, сколько стоит на деле."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    count = 0
    while loop.time() < deadline:
        count = await _waiters(watcher)
        if count >= expected:
            return count
        await asyncio.sleep(0.02)
    return count


async def _seed(db: AsyncSession, stub: StubAW) -> tuple[int, dict[str, Any]]:
    user = User(telegram_id=777, username='payer', language='ru', balance_kopeks=0)
    db.add(user)
    await db.commit()
    result = await _service().create_antarctic_payment(db, user_id=user.id, amount_kopeks=10000)
    assert result is not None
    await _service().start_antarctic_session(db, token=result['token'], id_token='login-1')
    assert len(stub.calls) == 1
    return user.id, result


async def _assert_credited_once(db: AsyncSession, user_id: int) -> None:
    await db.rollback()
    deposits = (
        await db.execute(
            select(func.count())
            .select_from(Transaction)
            .where(Transaction.payment_method == PaymentMethod.ANTARCTIC.value)
        )
    ).scalar_one()
    balance = (await db.execute(select(User.balance_kopeks).where(User.id == user_id))).scalar_one()
    payment = (await db.execute(select(AntarcticPayment).execution_options(populate_existing=True))).scalar_one()
    assert deposits == 1
    assert balance == 10000
    assert (payment.status, payment.is_paid) == ('success', True)
    assert payment.transaction_id is not None


async def test_concurrent_webhook_and_refresh_credit_once(postgres_database, monkeypatch) -> None:
    """Вебхук и сверка через API ждут одну блокировку: зачисление одно, оба вызова успешны."""
    _enable(monkeypatch)
    stub = StubAW()
    _install(monkeypatch, stub)
    async with postgres_sessions(postgres_database, TABLES, count=5) as (setup, holder, hook, check, watcher):
        user_id, result = await _seed(setup, stub)
        stub.status = 'approved'

        await get_antarctic_payment_by_id_for_update(holder, result['local_payment_id'])
        webhook = asyncio.create_task(_service().process_antarctic_webhook(hook, _event(result['token'])))
        refresh = asyncio.create_task(_service().refresh_antarctic_payment(check, result['local_payment_id']))
        waiting = await _wait_for_waiters(watcher, 2)
        await holder.rollback()
        webhook_result, refresh_result = await asyncio.gather(webhook, refresh)

        assert waiting == 2, 'оба соперника должны стоять в очереди за блокировкой — конкуренции не возникло'
        assert webhook_result is True
        assert refresh_result is not None
        assert refresh_result['is_paid'] is True
        await _assert_credited_once(setup, user_id)


async def test_concurrent_webhook_redelivery_credits_once(postgres_database, monkeypatch) -> None:
    """AW доставляет at-least-once: два одинаковых вебхука одновременно — одно зачисление."""
    _enable(monkeypatch)
    stub = StubAW()
    _install(monkeypatch, stub)
    async with postgres_sessions(postgres_database, TABLES, count=4) as (setup, holder, first, second):
        watcher = setup
        user_id, result = await _seed(setup, stub)

        await get_antarctic_payment_by_id_for_update(holder, result['local_payment_id'])
        payload = _event(result['token'])
        one = asyncio.create_task(_service().process_antarctic_webhook(first, payload))
        two = asyncio.create_task(_service().process_antarctic_webhook(second, payload))
        waiting = await _wait_for_waiters(watcher, 2)
        await holder.rollback()
        results = await asyncio.gather(one, two)

        assert waiting == 2, 'оба соперника должны стоять в очереди за блокировкой — конкуренции не возникло'
        assert results == [True, True]
        await _assert_credited_once(setup, user_id)


async def test_concurrent_sessions_from_two_wallets_create_one_intent(postgres_database, monkeypatch) -> None:
    """Ссылку открыли из двух кошельков одновременно: интент один, второй получает 409."""
    _enable(monkeypatch)
    stub = StubAW()
    _install(monkeypatch, stub)
    async with postgres_sessions(postgres_database, TABLES, count=3) as (setup, first, second):
        user = User(telegram_id=778, username='payer', language='ru', balance_kopeks=0)
        setup.add(user)
        await setup.commit()
        result = await _service().create_antarctic_payment(setup, user_id=user.id, amount_kopeks=10000)
        assert result is not None

        original = stub.upsert_pay_intent

        async def slow_upsert(**kwargs: Any):
            # Первая сессия держит блокировку на время «HTTP» — вторая обязана подождать и увидеть привязку.
            await asyncio.sleep(0.3)
            return await original(**kwargs)

        stub.upsert_pay_intent = slow_upsert  # type: ignore[method-assign]

        outcomes = await asyncio.gather(
            _service().start_antarctic_session(first, token=result['token'], id_token='login-1'),
            _service().start_antarctic_session(second, token=result['token'], id_token='login-2'),
            return_exceptions=True,
        )

        errors = [outcome for outcome in outcomes if isinstance(outcome, AntarcticSessionError)]
        sessions = [outcome for outcome in outcomes if isinstance(outcome, dict)]
        assert len(sessions) == 1
        assert [error.code for error in errors] == ['login_mismatch']
        assert len(stub.calls) == 1
        row = (await setup.execute(select(AntarcticPayment).execution_options(populate_existing=True))).scalar_one()
        assert row.user_app_login == stub.calls[0]['user_app_login']
        assert row.operation_id == 'op-1'


async def test_column_limits_hold_on_postgres(postgres_database, monkeypatch) -> None:
    """VARCHAR(n) на PostgreSQL проверяется: максимальные по длине login и operation_id проходят."""
    _enable(monkeypatch)
    login = 'l' * 64
    stub = StubAW(operation_id='o' * 64)
    _install(monkeypatch, stub)
    async with postgres_sessions(postgres_database, TABLES, count=1) as (db,):
        user = User(telegram_id=779, username='payer', language='ru', balance_kopeks=0)
        db.add(user)
        await db.commit()
        result = await _service().create_antarctic_payment(db, user_id=user.id, amount_kopeks=10000)
        assert result is not None

        session = await _service().start_antarctic_session(db, token=result['token'], id_token=login)

        assert session['operation_id'] == 'o' * 64
        row = (await db.execute(select(AntarcticPayment).execution_options(populate_existing=True))).scalar_one()
        assert row.user_app_login == login
