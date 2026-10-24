"""Antarctic Wallet: подключение к проверке платежей, конфигу методов и списку способов оплаты."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from app.config import settings
from app.database.models import AntarcticPayment, Base, PaymentMethod, User
from app.services import payment_method_config_service as method_config, payment_verification_service as verification
from app.utils.payment_utils import get_available_payment_methods, is_payment_method_available
from tests.fixtures.sqlite_memory import memory_session


TABLES = list(Base.metadata.sorted_tables)


def _enable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'ANTARCTIC_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'ANTARCTIC_APP_ID', '68019113', raising=False)
    monkeypatch.setattr(settings, 'ANTARCTIC_APP_SECRET', 'sk_test', raising=False)


def test_antarctic_in_check_sets(monkeypatch: pytest.MonkeyPatch) -> None:
    assert PaymentMethod.ANTARCTIC in verification.SUPPORTED_MANUAL_CHECK_METHODS
    assert PaymentMethod.ANTARCTIC in verification.SUPPORTED_AUTO_CHECK_METHODS

    monkeypatch.setattr(settings, 'ANTARCTIC_DISPLAY_NAME', 'AW custom', raising=False)
    assert verification.method_display_name(PaymentMethod.ANTARCTIC) == 'AW custom'

    monkeypatch.setattr(settings, 'ANTARCTIC_ENABLED', False, raising=False)
    assert verification._method_is_enabled(PaymentMethod.ANTARCTIC) is False
    _enable(monkeypatch)
    assert verification._method_is_enabled(PaymentMethod.ANTARCTIC) is True


async def _seed(db, *, token: str, is_paid: bool, status: str) -> AntarcticPayment:
    user = User(telegram_id=555, username='payer', language='ru', balance_kopeks=0)
    db.add(user)
    await db.flush()
    payment = AntarcticPayment(
        user_id=user.id,
        token=token,
        amount_kopeks=10000,
        amount_usd_cents=105,
        usd_rub_rate=95.5,
        status=status,
        is_paid=is_paid,
        created_at=datetime.now(UTC),
    )
    db.add(payment)
    await db.commit()
    return payment


@pytest.mark.asyncio
async def test_pending_rows_listed_and_paid_excluded(monkeypatch: pytest.MonkeyPatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        pending = await _seed(db, token='tok-pending', is_paid=False, status='pending')
        await _seed_extra(db, pending.user_id)

        records = await verification.list_recent_pending_payments(db)
        antarctic = [r for r in records if r.method == PaymentMethod.ANTARCTIC]
        assert [r.identifier for r in antarctic] == ['tok-pending']

        record = await verification.get_payment_record(db, PaymentMethod.ANTARCTIC, pending.id)
        assert record is not None
        assert record.identifier == 'tok-pending'
        assert record.amount_kopeks == 10000


async def _seed_extra(db, user_id: int) -> None:
    db.add(
        AntarcticPayment(
            user_id=user_id,
            token='tok-paid',
            amount_kopeks=10000,
            amount_usd_cents=105,
            usd_rub_rate=95.5,
            status='paid',
            is_paid=True,
            created_at=datetime.now(UTC),
        )
    )
    db.add(
        AntarcticPayment(
            user_id=user_id,
            token='tok-expired',
            amount_kopeks=10000,
            amount_usd_cents=105,
            usd_rub_rate=95.5,
            status='expired',
            is_paid=False,
            created_at=datetime.now(UTC),
        )
    )
    await db.commit()


@pytest.mark.asyncio
async def test_manual_check_calls_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        payment = await _seed(db, token='tok-manual', is_paid=False, status='pending')
        calls: list[int] = []

        class StubService:
            async def refresh_antarctic_payment(self, session: Any, local_payment_id: int) -> dict[str, Any]:
                calls.append(local_payment_id)
                return {'payment': payment, 'status': 'pending', 'is_paid': False}

        record = await verification.run_manual_check(db, PaymentMethod.ANTARCTIC, payment.id, StubService())

        assert calls == [payment.id]
        assert record is not None
        assert record.identifier == 'tok-manual'


def test_method_defaults_and_description_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)
    defaults = method_config._get_method_defaults()['antarctic']
    assert defaults['default_display_name'] == settings.get_antarctic_display_name()
    assert defaults['is_configured'] is True
    assert defaults['default_min'] == settings.ANTARCTIC_MIN_AMOUNT_KOPEKS
    assert defaults['default_max'] == settings.ANTARCTIC_MAX_AMOUNT_KOPEKS
    assert defaults['default_description'] == settings.ANTARCTIC_DESCRIPTION
    assert 'antarctic' in method_config.DEFAULT_METHOD_ORDER


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('stored', 'expected'),
    [(None, 'ANTARCTIC_DESCRIPTION'), ('', 'ANTARCTIC_DESCRIPTION'), ('Своё описание', 'Своё описание')],
)
async def test_enabled_methods_description(monkeypatch: pytest.MonkeyPatch, stored: str | None, expected: str) -> None:
    _enable(monkeypatch)
    if expected == 'ANTARCTIC_DESCRIPTION':
        expected = settings.ANTARCTIC_DESCRIPTION

    config = SimpleNamespace(
        method_id='antarctic',
        is_enabled=True,
        display_name=None,
        description=stored,
        min_amount_kopeks=None,
        max_amount_kopeks=None,
        sub_options=None,
        quick_amounts=None,
        sort_order=1,
        user_type_filter='all',
        first_topup_filter='any',
        promo_group_filter_mode='all',
        allowed_promo_groups=[],
        open_url_direct=False,
    )

    async def fake_get_all_configs(_db: Any) -> list[Any]:
        return [config]

    monkeypatch.setattr(method_config, 'get_all_configs', fake_get_all_configs)
    methods = await method_config.get_enabled_methods_for_user(None, None)

    assert len(methods) == 1
    assert methods[0]['id'] == 'antarctic'
    assert methods[0]['description'] == expected


def test_payment_utils_lists_antarctic_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'ANTARCTIC_ENABLED', False, raising=False)
    assert 'antarctic' not in [m['id'] for m in get_available_payment_methods()]
    assert is_payment_method_available('antarctic') is False

    _enable(monkeypatch)
    entry = next(m for m in get_available_payment_methods() if m['id'] == 'antarctic')
    assert entry['icon'] == '🐧'
    assert entry['description'] == settings.ANTARCTIC_DESCRIPTION
    assert entry['callback'] == 'topup_antarctic'
    assert entry['name'] == settings.get_antarctic_display_name()
    assert is_payment_method_available('antarctic') is True
