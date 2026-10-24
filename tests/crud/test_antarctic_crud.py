"""CRUD платежей Antarctic Wallet."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from app.database.crud.antarctic import (
    create_antarctic_payment,
    get_antarctic_payment_by_id,
    get_antarctic_payment_by_id_for_update,
    get_antarctic_payment_by_operation_id,
    get_antarctic_payment_by_token,
    link_antarctic_payment_to_transaction,
)
from app.database.models import AntarcticPayment
from tests.fixtures.sqlite_memory import memory_session


TABLES = [AntarcticPayment.__table__]


async def _create(db, token_value, cents=105):
    return await create_antarctic_payment(
        db,
        user_id=None,
        token=token_value,
        amount_kopeks=10000,
        amount_usd_cents=cents,
        usd_rub_rate=Decimal('95.1234'),
        description='top up',
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
        metadata_json={'a': 1},
    )


async def test_roundtrip(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        p = await _create(db, 'tok1')
        assert p.status == 'pending'
        assert p.is_paid is False
        assert p.is_pending is True
        assert p.amount_rubles == 100.0
        assert (await get_antarctic_payment_by_token(db, 'tok1')).id == p.id
        assert (await get_antarctic_payment_by_id(db, p.id)).token == 'tok1'
        assert (await get_antarctic_payment_by_id_for_update(db, p.id)).id == p.id
        assert await get_antarctic_payment_by_token(db, 'nope') is None

        p.operation_id = 'op-1'
        await db.commit()
        assert (await get_antarctic_payment_by_operation_id(db, 'op-1')).id == p.id
        assert await get_antarctic_payment_by_operation_id(db, 'op-2') is None


async def test_duplicate_token_raises(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        await _create(db, 'tok1')
        with pytest.raises(IntegrityError):
            await _create(db, 'tok1')


async def test_link_transaction(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        p = await _create(db, 'tok1')
        p = await link_antarctic_payment_to_transaction(db, p, 7)
        assert p.transaction_id == 7


@pytest.mark.parametrize(('cents', 'expected'), [(105, '1.05'), (5, '0.05'), (1000, '10.00')])
def test_amount_usd(cents, expected):
    assert AntarcticPayment(amount_usd_cents=cents).amount_usd == expected
