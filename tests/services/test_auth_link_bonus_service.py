"""Бонус за привязку второго способа входа: правила выдачи, «платящий» аккаунт, атомарность.

Идёт по настоящей БД (in-memory SQLite со схемой модели): нужны реальные запросы, уникальность
``auth_link_bonus.user_id`` и откат savepoint. Конкуренция на настоящем PostgreSQL —
``tests/database/test_auth_link_bonus_postgres.py``.
"""

from __future__ import annotations

import functools
import inspect
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.config import settings
from app.database.models import (
    AuthLinkBonus,
    PaymentMethod,
    SentNotification,
    Subscription,
    SubscriptionStatus,
    Tariff,
    Transaction,
    TransactionType,
    User,
    UserPasskey,
)
from app.services import auth_link_bonus_service as service
from tests.fixtures.sqlite_memory import memory_session


TABLES = (
    User.__table__,
    UserPasskey.__table__,
    Subscription.__table__,
    Tariff.__table__,
    Transaction.__table__,
    AuthLinkBonus.__table__,
    SentNotification.__table__,
)

pytestmark = pytest.mark.asyncio

DAYS = 7


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setattr(settings, 'AUTH_LINK_BONUS_ENABLED', True)
    monkeypatch.setattr(settings, 'AUTH_LINK_BONUS_DAYS', DAYS)
    monkeypatch.setattr(settings, 'AUTH_LINK_BONUS_REQUIRES_PAID', True)


def uses_db(test):
    """Тесту выдаётся ``db`` — сессия к свежей in-memory БД (асинхронные фикстуры здесь не поддерживаются)."""
    signature = inspect.signature(test)
    params = [p for p in signature.parameters.values() if p.name != 'db']
    if 'monkeypatch' not in signature.parameters:
        params.append(inspect.Parameter('monkeypatch', inspect.Parameter.POSITIONAL_OR_KEYWORD))

    @functools.wraps(test)
    async def wrapper(*args, **kwargs):
        monkeypatch = kwargs['monkeypatch']
        passed = {k: v for k, v in kwargs.items() if k in signature.parameters}
        async with memory_session(monkeypatch, TABLES) as db:
            await test(*args, db=db, **passed)

    wrapper.__signature__ = signature.replace(parameters=params)
    return wrapper


async def make_user(db, **fields) -> User:
    user = User(telegram_id=fields.pop('telegram_id', None), username='u', status='active', language='ru', **fields)
    db.add(user)
    await db.commit()
    return user


async def pay(db, user, *, type_=TransactionType.DEPOSIT, method=PaymentMethod.YOOKASSA.value, completed=True):
    db.add(
        Transaction(
            user_id=user.id, type=type_.value, amount_kopeks=10000, payment_method=method, is_completed=completed
        )
    )
    await db.commit()


async def subscribe(db, user, *, days=30, trial=False, status=SubscriptionStatus.ACTIVE.value, tariff_id=None):
    sub = Subscription(
        user_id=user.id,
        status=status,
        is_trial=trial,
        end_date=datetime.now(UTC) + timedelta(days=days),
        tariff_id=tariff_id,
        remnawave_short_id=uuid.uuid4().hex[:12],  # UNIQUE NOT NULL: в проде его проставляет CRUD
    )
    db.add(sub)
    await db.commit()
    return sub


async def rows(db) -> list[AuthLinkBonus]:
    return list((await db.execute(select(AuthLinkBonus).execution_options(populate_existing=True))).scalars())


async def end_date(db, sub) -> datetime:
    value = (await db.execute(select(Subscription.end_date).where(Subscription.id == sub.id))).scalar_one()
    return value if value.tzinfo else value.replace(tzinfo=UTC)


async def paying_user(db, **sub_kwargs):
    user = await make_user(db)
    await pay(db, user)
    sub = await subscribe(db, user, **sub_kwargs)
    return user, sub


# ---------------------------------------------------------------------------
# Что считается платящим аккаунтом
# ---------------------------------------------------------------------------


@uses_db
async def test_account_without_transactions_has_no_payment_history(db) -> None:
    user = await make_user(db)
    assert await service.has_payment_history(db, user.id) is False


@pytest.mark.parametrize('type_', [TransactionType.DEPOSIT, TransactionType.SUBSCRIPTION_PAYMENT])
@uses_db
async def test_a_completed_gateway_payment_is_payment_history(db, type_) -> None:
    user = await make_user(db)
    await pay(db, user, type_=type_)
    assert await service.has_payment_history(db, user.id) is True


@pytest.mark.parametrize(
    ('type_', 'method', 'completed'),
    [
        (TransactionType.DEPOSIT, PaymentMethod.MANUAL.value, True),  # начислил админ
        (TransactionType.SUBSCRIPTION_PAYMENT, PaymentMethod.BALANCE.value, True),  # с баланса: деньги не из шлюза
        (TransactionType.DEPOSIT, None, True),  # промокод, подарок, баланс без шлюза
        (TransactionType.DEPOSIT, PaymentMethod.YOOKASSA.value, False),  # платёж не прошёл
        (TransactionType.REFERRAL_REWARD, PaymentMethod.YOOKASSA.value, True),
        (TransactionType.REFUND, PaymentMethod.YOOKASSA.value, True),
    ],
)
@uses_db
async def test_other_transactions_are_not_payment_history(db, type_, method, completed) -> None:
    user = await make_user(db)
    await pay(db, user, type_=type_, method=method, completed=completed)
    assert await service.has_payment_history(db, user.id) is False


@uses_db
async def test_a_trial_only_account_has_no_payment_history(db) -> None:
    user = await make_user(db)
    await subscribe(db, user, trial=True, status=SubscriptionStatus.TRIAL.value)
    assert await service.has_payment_history(db, user.id) is False


@uses_db
async def test_payments_of_another_account_do_not_count(db) -> None:
    payer = await make_user(db)
    other = await make_user(db)
    await pay(db, payer)
    assert await service.has_payment_history(db, other.id) is False


# ---------------------------------------------------------------------------
# Какие способы входа считаются
# ---------------------------------------------------------------------------


@uses_db
async def test_account_without_extra_methods_has_no_qualifying_method(db) -> None:
    user = await make_user(db, telegram_id=1001, email='a@example.com', password_hash='x')
    assert await service.has_qualifying_method(db, user) is False, 'Telegram и почта не считаются'


@pytest.mark.parametrize('column', ['google_id', 'yandex_id', 'discord_id'])
@uses_db
async def test_a_linked_oauth_provider_is_a_qualifying_method(db, column) -> None:
    user = await make_user(db, **{column: 'sub-1'})
    assert await service.has_qualifying_method(db, user) is True


@uses_db
async def test_a_registered_passkey_is_a_qualifying_method(db) -> None:
    user = await make_user(db)
    db.add(UserPasskey(user_id=user.id, credential_id=b'c1', public_key=b'k', device_type='single_device', name='Key'))
    await db.commit()
    assert await service.has_qualifying_method(db, user) is True


# ---------------------------------------------------------------------------
# Выдача
# ---------------------------------------------------------------------------


@uses_db
async def test_grant_extends_the_paid_subscription_and_records_the_row(db) -> None:
    user, sub = await paying_user(db, days=30)
    before = await end_date(db, sub)

    grant = await service.grant_link_bonus(db, user, 'passkey', had_qualifying_method=False)
    await db.commit()

    assert grant is not None and grant.days == DAYS and grant.method == 'passkey'
    assert grant.subscription_id == sub.id
    assert await end_date(db, sub) == before + timedelta(days=DAYS)
    (row,) = await rows(db)
    assert (row.user_id, row.method) == (user.id, 'passkey') and row.granted_at is not None


@uses_db
async def test_grant_sets_the_panel_expiry_marker_and_clears_sent_expiry_notices(db) -> None:
    user, sub = await paying_user(db)
    db.add(SentNotification(user_id=user.id, subscription_id=sub.id, notification_type='expiring_3d'))
    await db.commit()

    await service.grant_link_bonus(db, user, 'google', had_qualifying_method=False)
    await db.commit()

    refreshed = (
        await db.execute(
            select(Subscription).where(Subscription.id == sub.id).execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert refreshed.pending_panel_expire_at is not None
    assert (await db.execute(select(func.count()).select_from(SentNotification))).scalar_one() == 0


@uses_db
async def test_the_tariff_traffic_rule_is_applied_like_on_any_renewal(db, monkeypatch) -> None:
    """Правило репозитория: любое продление даты применяет правило трафика тарифа (см. сторож в tests/contracts)."""
    from unittest.mock import AsyncMock

    applied = AsyncMock()
    monkeypatch.setattr(service, 'reconcile_tariff_traffic_limit', applied)
    user, sub = await paying_user(db)

    grant = await service.grant_link_bonus(db, user, 'passkey', had_qualifying_method=False)

    assert grant is not None
    applied.assert_awaited_once()
    assert applied.await_args.args[1].id == sub.id


@uses_db
async def test_a_failure_applying_the_traffic_rule_rolls_the_whole_grant_back(db, monkeypatch) -> None:
    async def _boom(*args, **kwargs):
        raise RuntimeError('housekeeping failed')

    monkeypatch.setattr(service, 'reconcile_tariff_traffic_limit', _boom)
    user, sub = await paying_user(db)
    sub_id = sub.id
    before = await end_date(db, sub)

    assert await service.grant_link_bonus(db, user, 'passkey', had_qualifying_method=False) is None
    await db.commit()

    assert await end_date(db, SimpleNamespace(id=sub_id)) == before and await rows(db) == []


@uses_db
async def test_a_grant_is_visible_to_the_admin_in_the_log(db) -> None:
    from structlog.testing import capture_logs

    user, sub = await paying_user(db)

    with capture_logs() as logs:
        await service.grant_link_bonus(db, user, 'google', had_qualifying_method=False)

    (entry,) = [e for e in logs if e['event'] == 'Link bonus granted']
    assert entry['log_level'] == 'info'
    assert (entry['user_id'], entry['method'], entry['days'], entry['subscription_id']) == (
        user.id,
        'google',
        DAYS,
        sub.id,
    )


@uses_db
async def test_nothing_is_granted_when_the_feature_is_off(db, monkeypatch) -> None:
    monkeypatch.setattr(settings, 'AUTH_LINK_BONUS_ENABLED', False)
    user, sub = await paying_user(db)
    before = await end_date(db, sub)

    assert await service.grant_link_bonus(db, user, 'passkey', had_qualifying_method=False) is None
    await db.commit()

    assert await end_date(db, sub) == before and await rows(db) == []


@uses_db
async def test_a_second_method_does_not_grant(db) -> None:
    user, sub = await paying_user(db)
    before = await end_date(db, sub)

    assert await service.grant_link_bonus(db, user, 'google', had_qualifying_method=True) is None
    await db.commit()

    assert await end_date(db, sub) == before and await rows(db) == []


@uses_db
async def test_the_bonus_is_never_repeated(db) -> None:
    user, sub = await paying_user(db, days=30)
    before = await end_date(db, sub)

    first = await service.grant_link_bonus(db, user, 'passkey', had_qualifying_method=False)
    await db.commit()
    again = await service.grant_link_bonus(db, user, 'google', had_qualifying_method=False)
    await db.commit()

    assert first is not None and again is None
    assert await end_date(db, sub) == before + timedelta(days=DAYS)
    assert len(await rows(db)) == 1


@uses_db
async def test_relink_after_unlink_does_not_repeat_the_bonus(db) -> None:
    """Отвязал и привязал снова: строка выдачи осталась, второй раз бонуса нет."""
    user, sub = await paying_user(db, days=30)
    before = await end_date(db, sub)
    await service.grant_link_bonus(db, user, 'google', had_qualifying_method=False)
    await db.commit()

    for method in ('google', 'passkey', 'google'):
        assert await service.grant_link_bonus(db, user, method, had_qualifying_method=False) is None
        await db.commit()

    assert await end_date(db, sub) == before + timedelta(days=DAYS)


@pytest.mark.parametrize('configured', [-5, 0, 1, 7, 365, 366, 100000])
@uses_db
async def test_days_are_clamped_to_the_bounds(db, monkeypatch, configured) -> None:
    monkeypatch.setattr(settings, 'AUTH_LINK_BONUS_DAYS', configured)
    user, sub = await paying_user(db, days=30)
    before = await end_date(db, sub)

    grant = await service.grant_link_bonus(db, user, 'passkey', had_qualifying_method=False)
    await db.commit()

    effective = min(max(configured, 1), 365)
    assert grant is not None and grant.days == effective
    assert await end_date(db, sub) == before + timedelta(days=effective)


# -- платящий аккаунт ----------------------------------------------------


@uses_db
async def test_requires_paid_refuses_an_account_with_only_a_trial(db) -> None:
    user = await make_user(db)
    sub = await subscribe(db, user, trial=True, status=SubscriptionStatus.TRIAL.value)
    before = await end_date(db, sub)

    assert await service.grant_link_bonus(db, user, 'passkey', had_qualifying_method=False) is None
    await db.commit()

    assert await end_date(db, sub) == before and await rows(db) == []


@uses_db
async def test_requires_paid_refuses_a_subscription_given_without_payment(db) -> None:
    user = await make_user(db)
    sub = await subscribe(db, user, days=30)  # выдал админ или промокод: платежа нет
    before = await end_date(db, sub)

    assert await service.grant_link_bonus(db, user, 'passkey', had_qualifying_method=False) is None
    await db.commit()

    assert await end_date(db, sub) == before and await rows(db) == []


@uses_db
async def test_without_requires_paid_any_active_paid_subscription_qualifies(db, monkeypatch) -> None:
    monkeypatch.setattr(settings, 'AUTH_LINK_BONUS_REQUIRES_PAID', False)
    user = await make_user(db)
    sub = await subscribe(db, user, days=30)
    before = await end_date(db, sub)

    grant = await service.grant_link_bonus(db, user, 'passkey', had_qualifying_method=False)
    await db.commit()

    assert grant is not None
    assert await end_date(db, sub) == before + timedelta(days=DAYS)


@uses_db
async def test_without_requires_paid_a_trial_is_still_not_extended(db, monkeypatch) -> None:
    monkeypatch.setattr(settings, 'AUTH_LINK_BONUS_REQUIRES_PAID', False)
    user = await make_user(db)
    await subscribe(db, user, trial=True, status=SubscriptionStatus.TRIAL.value)

    assert await service.grant_link_bonus(db, user, 'passkey', had_qualifying_method=False) is None
    assert await rows(db) == []


# -- нечего продлевать: право не тратится ----------------------------------


@uses_db
async def test_without_an_active_paid_subscription_nothing_is_granted_and_the_right_is_kept(db) -> None:
    user = await make_user(db)
    await pay(db, user)
    assert await service.grant_link_bonus(db, user, 'passkey', had_qualifying_method=False) is None
    await db.commit()
    assert await rows(db) == [], 'право на бонус не потрачено'

    sub = await subscribe(db, user, days=30)
    before = await end_date(db, sub)
    grant = await service.grant_link_bonus(db, user, 'google', had_qualifying_method=False)
    await db.commit()

    assert grant is not None
    assert await end_date(db, sub) == before + timedelta(days=DAYS)


@pytest.mark.parametrize(
    'status',
    [SubscriptionStatus.EXPIRED.value, SubscriptionStatus.DISABLED.value, SubscriptionStatus.LIMITED.value],
)
@uses_db
async def test_a_subscription_that_is_not_active_is_not_extended(db, status) -> None:
    user = await make_user(db)
    await pay(db, user)
    sub = await subscribe(db, user, days=30, status=status)
    before = await end_date(db, sub)

    assert await service.grant_link_bonus(db, user, 'passkey', had_qualifying_method=False) is None
    await db.commit()

    assert await end_date(db, sub) == before and await rows(db) == []


@uses_db
async def test_an_expired_by_date_subscription_is_not_extended(db) -> None:
    user = await make_user(db)
    await pay(db, user)
    sub = await subscribe(db, user, days=-1)
    before = await end_date(db, sub)

    assert await service.grant_link_bonus(db, user, 'passkey', had_qualifying_method=False) is None
    await db.commit()

    assert await end_date(db, sub) == before and await rows(db) == []


@uses_db
async def test_a_daily_tariff_subscription_is_not_extended(db) -> None:
    user = await make_user(db)
    await pay(db, user)
    tariff = Tariff(name='Daily', is_daily=True)
    db.add(tariff)
    await db.commit()
    sub = await subscribe(db, user, days=1, tariff_id=tariff.id)
    before = await end_date(db, sub)

    assert await service.grant_link_bonus(db, user, 'passkey', had_qualifying_method=False) is None
    await db.commit()

    assert await end_date(db, sub) == before and await rows(db) == []


@uses_db
async def test_with_several_subscriptions_the_paid_one_with_the_latest_end_is_extended(db) -> None:
    user = await make_user(db)
    await pay(db, user)
    trial = await subscribe(db, user, days=2, trial=True, status=SubscriptionStatus.TRIAL.value)
    short = await subscribe(db, user, days=10)
    long = await subscribe(db, user, days=90)
    trial_before = await end_date(db, trial)
    short_before = await end_date(db, short)
    long_before = await end_date(db, long)

    grant = await service.grant_link_bonus(db, user, 'passkey', had_qualifying_method=False)
    await db.commit()

    assert grant is not None and grant.subscription_id == long.id
    assert await end_date(db, long) == long_before + timedelta(days=DAYS)
    assert await end_date(db, short) == short_before
    assert await end_date(db, trial) == trial_before


# -- атомарность ---------------------------------------------------------


@uses_db
async def test_a_row_that_appeared_first_refuses_the_grant_without_touching_the_subscription(db) -> None:
    """Проигравший гонку: строка уже есть (её вставил соперник), продления нет."""
    user, sub = await paying_user(db, days=30)
    before = await end_date(db, sub)
    db.add(AuthLinkBonus(user_id=user.id, method='google'))
    await db.commit()

    assert await service.grant_link_bonus(db, user, 'passkey', had_qualifying_method=False) is None
    await db.commit()

    assert await end_date(db, sub) == before
    assert [row.method for row in await rows(db)] == ['google']


@uses_db
async def test_unique_violation_on_insert_is_swallowed_and_leaves_the_session_usable(db, monkeypatch) -> None:
    """Предпроверка «строки ещё нет» прошла, а вставка упала на UNIQUE: то же, что гонка под PostgreSQL."""
    user, sub = await paying_user(db, days=30)
    before = await end_date(db, sub)
    db.add(AuthLinkBonus(user_id=user.id, method='google'))
    await db.commit()

    async def _not_yet(_db, _user_id) -> bool:
        return False

    monkeypatch.setattr(service, 'has_received_bonus', _not_yet)

    assert await service.grant_link_bonus(db, user, 'passkey', had_qualifying_method=False) is None
    await db.commit()

    assert await end_date(db, sub) == before
    assert len(await rows(db)) == 1


@uses_db
async def test_a_failure_while_extending_rolls_back_the_row_and_keeps_the_outer_work(db, monkeypatch) -> None:
    user, sub = await paying_user(db, days=30)
    sub_id = sub.id  # после отката savepoint объект подписки протух: читаем по id
    before = await end_date(db, sub)

    async def _boom(*args, **kwargs):
        raise RuntimeError('cleanup failed')

    monkeypatch.setattr(service, 'clear_notifications', _boom)
    outer = User(telegram_id=None, username='outer', status='active', language='ru')
    db.add(outer)  # работа ссылки, ожидающая коммита: savepoint её не трогает

    assert await service.grant_link_bonus(db, user, 'passkey', had_qualifying_method=False) is None
    await db.commit()

    assert await end_date(db, SimpleNamespace(id=sub_id)) == before
    assert await rows(db) == [], 'строка выдачи откатилась вместе с продлением'
    assert (await db.execute(select(func.count()).select_from(User).where(User.username == 'outer'))).scalar_one() == 1


# ---------------------------------------------------------------------------
# Статус для баннера
# ---------------------------------------------------------------------------


@uses_db
async def test_status_is_available_for_a_paying_account_with_no_extra_method(db) -> None:
    user, _ = await paying_user(db)
    status = await service.get_link_bonus_status(db, user)
    assert (status.available, status.days) == (True, DAYS)


@uses_db
async def test_status_is_off_when_the_feature_is_disabled(db, monkeypatch) -> None:
    user, _ = await paying_user(db)
    monkeypatch.setattr(settings, 'AUTH_LINK_BONUS_ENABLED', False)
    status = await service.get_link_bonus_status(db, user)
    assert (status.available, status.days) == (False, 0)


@uses_db
async def test_status_is_unavailable_once_the_bonus_was_received(db) -> None:
    user, _ = await paying_user(db)
    await service.grant_link_bonus(db, user, 'passkey', had_qualifying_method=False)
    await db.commit()
    assert (await service.get_link_bonus_status(db, user)).available is False


@uses_db
async def test_status_is_unavailable_when_a_qualifying_method_is_already_linked(db) -> None:
    user, _ = await paying_user(db)
    user.google_id = 'sub-1'
    await db.commit()
    assert (await service.get_link_bonus_status(db, user)).available is False


@uses_db
async def test_status_is_unavailable_for_a_trial_only_account(db) -> None:
    user = await make_user(db)
    await subscribe(db, user, trial=True, status=SubscriptionStatus.TRIAL.value)
    assert (await service.get_link_bonus_status(db, user)).available is False


@uses_db
async def test_status_is_unavailable_when_there_is_nothing_to_extend(db) -> None:
    """Платил, но подписка кончилась: привязка ничего не даст, баннер не обещает."""
    user = await make_user(db)
    await pay(db, user)
    assert (await service.get_link_bonus_status(db, user)).available is False


@uses_db
async def test_status_ignores_payment_history_when_requires_paid_is_off(db, monkeypatch) -> None:
    monkeypatch.setattr(settings, 'AUTH_LINK_BONUS_REQUIRES_PAID', False)
    user = await make_user(db)
    await subscribe(db, user, days=30)
    assert (await service.get_link_bonus_status(db, user)).available is True
