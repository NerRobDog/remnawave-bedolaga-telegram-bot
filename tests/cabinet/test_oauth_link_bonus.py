"""Бонус за привязку OAuth-провайдера: явная привязка из профиля и привязка при входе по подтверждённой почте.

Настоящая БД (in-memory SQLite со схемой модели); провайдер и Redis подменены. Бонус выдаётся тем же
коммитом, что и привязка, и никогда не ломает её.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.cabinet.routes import account_linking, oauth as oauth_routes
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
    UserStatus,
)
from app.services import auth_link_bonus_service as service
from tests.fixtures.sqlite_memory import memory_session


pytestmark = pytest.mark.asyncio

TABLES = (
    User.__table__,
    UserPasskey.__table__,
    Subscription.__table__,
    Tariff.__table__,
    Transaction.__table__,
    AuthLinkBonus.__table__,
    SentNotification.__table__,
)
DAYS = 7


@pytest.fixture(autouse=True)
def bonus_on(monkeypatch) -> AsyncMock:
    monkeypatch.setattr(settings, 'AUTH_LINK_BONUS_ENABLED', True)
    monkeypatch.setattr(settings, 'AUTH_LINK_BONUS_DAYS', DAYS)
    monkeypatch.setattr(settings, 'AUTH_LINK_BONUS_REQUIRES_PAID', True)
    delivered = AsyncMock()
    monkeypatch.setattr(account_linking, 'deliver_link_bonus', delivered)
    monkeypatch.setattr(oauth_routes, 'deliver_link_bonus', delivered)
    return delivered


async def _paying_user(db, *, days=30, paid=True, **fields) -> tuple[User, int]:
    user = User(telegram_id=fields.pop('telegram_id', 7001), username='u', status=UserStatus.ACTIVE.value, **fields)
    db.add(user)
    await db.flush()
    if paid:
        db.add(
            Transaction(
                user_id=user.id,
                type=TransactionType.DEPOSIT.value,
                amount_kopeks=10000,
                payment_method=PaymentMethod.YOOKASSA.value,
                is_completed=True,
            )
        )
    sub = Subscription(
        user_id=user.id,
        status=SubscriptionStatus.ACTIVE.value,
        is_trial=False,
        end_date=datetime.now(UTC).replace(microsecond=0) + timedelta(days=days),
        remnawave_short_id=uuid.uuid4().hex[:12],
    )
    db.add(sub)
    await db.commit()
    return user, sub.id


async def _end(db, sub_id) -> datetime:
    value = (await db.execute(select(Subscription.end_date).where(Subscription.id == sub_id))).scalar_one()
    return value if value.tzinfo else value.replace(tzinfo=UTC)


async def _bonus_rows(db) -> list[AuthLinkBonus]:
    return list((await db.execute(select(AuthLinkBonus).execution_options(populate_existing=True))).scalars())


def _provider(provider_id: str = 'G1', email: str | None = None, email_verified: bool = False) -> MagicMock:
    prov = MagicMock()
    prov.exchange_code = AsyncMock(return_value={'access_token': 'x'})
    prov.get_user_info = AsyncMock(
        return_value=SimpleNamespace(provider_id=provider_id, email=email, email_verified=email_verified)
    )
    return prov


async def _link(db, user, provider='google', *, provider_id='G1', owner=None, merge=False):
    patches = [
        patch.object(account_linking, 'get_provider', MagicMock(return_value=_provider(provider_id))),
        patch.object(account_linking, 'get_user_by_oauth_provider', AsyncMock(return_value=owner)),
        patch.object(account_linking, 'get_user_by_email', AsyncMock(return_value=None)),
        patch.object(account_linking, 'create_merge_token', AsyncMock(return_value='merge-token')),
    ]
    for p in patches:
        p.start()
    try:
        return await account_linking._exchange_and_link_oauth(
            db=db,
            user=user,
            provider=provider,
            code='code',
            state='state',
            state_data={},
            device_id=None,
            log_context='test',
        )
    finally:
        for p in patches:
            p.stop()


def uses_db(test):
    """Тесту выдаётся ``db`` — сессия к свежей in-memory БД."""
    import functools
    import inspect

    signature = inspect.signature(test)
    params = [p for p in signature.parameters.values() if p.name != 'db']
    if 'monkeypatch' not in signature.parameters:
        params.append(inspect.Parameter('monkeypatch', inspect.Parameter.POSITIONAL_OR_KEYWORD))

    @functools.wraps(test)
    async def wrapper(*args, **kwargs):
        passed = {k: v for k, v in kwargs.items() if k in signature.parameters}
        async with memory_session(kwargs['monkeypatch'], TABLES) as db:
            await test(*args, db=db, **passed)

    wrapper.__signature__ = signature.replace(parameters=params)
    return wrapper


# ---------------------------------------------------------------------------
# Явная привязка из профиля (link/{provider}/callback и link/server-complete)
# ---------------------------------------------------------------------------


@uses_db
async def test_the_first_provider_link_extends_the_subscription_once(db, bonus_on) -> None:
    user, sub_id = await _paying_user(db)
    before = await _end(db, sub_id)

    result = await _link(db, user)

    assert (result.success, result.message) == (True, 'linked')
    assert await _end(db, sub_id) == before + timedelta(days=DAYS)
    (row,) = await _bonus_rows(db)
    assert (row.user_id, row.method) == (user.id, 'google')
    assert user.google_id == 'G1'
    bonus_on.assert_awaited_once()
    assert bonus_on.await_args.args[2].days == DAYS


@uses_db
async def test_a_second_provider_does_not_repeat_the_bonus(db, bonus_on) -> None:
    user, sub_id = await _paying_user(db)
    before = await _end(db, sub_id)

    await _link(db, user, 'google', provider_id='G1')
    again = await _link(db, user, 'yandex', provider_id='Y1')

    assert again.success is True
    assert await _end(db, sub_id) == before + timedelta(days=DAYS)
    assert len(await _bonus_rows(db)) == 1
    bonus_on.assert_awaited_once()


@uses_db
async def test_unlink_and_link_again_does_not_repeat_the_bonus(db) -> None:
    user, sub_id = await _paying_user(db, email='a@example.com', password_hash='x')
    before = await _end(db, sub_id)

    await _link(db, user, 'google', provider_id='G1')
    user.google_id = None  # отвязал
    await db.commit()
    await _link(db, user, 'google', provider_id='G1')

    assert await _end(db, sub_id) == before + timedelta(days=DAYS)
    assert len(await _bonus_rows(db)) == 1


@uses_db
async def test_linking_after_a_passkey_gets_no_bonus(db) -> None:
    user, sub_id = await _paying_user(db)
    db.add(UserPasskey(user_id=user.id, credential_id=b'c', public_key=b'k', device_type='single_device', name='K'))
    await db.commit()
    before = await _end(db, sub_id)

    result = await _link(db, user)

    assert result.success is True
    assert await _end(db, sub_id) == before and await _bonus_rows(db) == []


@uses_db
async def test_the_feature_is_off_by_default(db, monkeypatch, bonus_on) -> None:
    monkeypatch.setattr(settings, 'AUTH_LINK_BONUS_ENABLED', False)
    user, sub_id = await _paying_user(db)
    before = await _end(db, sub_id)

    result = await _link(db, user)

    assert result.success is True
    assert await _end(db, sub_id) == before and await _bonus_rows(db) == []
    bonus_on.assert_not_awaited()


@uses_db
async def test_no_bonus_without_payment_history(db) -> None:
    user, sub_id = await _paying_user(db, paid=False)
    before = await _end(db, sub_id)

    result = await _link(db, user)

    assert result.success is True
    assert await _end(db, sub_id) == before and await _bonus_rows(db) == []


@uses_db
async def test_already_linked_is_not_a_new_link(db, bonus_on) -> None:
    user, sub_id = await _paying_user(db, google_id='G1')
    before = await _end(db, sub_id)

    result = await _link(db, user, provider_id='G1')

    assert result.message == 'already_linked'
    assert await _end(db, sub_id) == before and await _bonus_rows(db) == []
    bonus_on.assert_not_awaited()


@uses_db
async def test_a_different_account_of_the_same_provider_is_refused_without_a_bonus(db, bonus_on) -> None:
    user, sub_id = await _paying_user(db, google_id='G-old')
    before = await _end(db, sub_id)

    with pytest.raises(HTTPException) as refused:
        await _link(db, user, provider_id='G-new')

    assert refused.value.status_code == 409
    assert await _end(db, sub_id) == before and await _bonus_rows(db) == []
    bonus_on.assert_not_awaited()


@uses_db
async def test_an_identity_owned_by_another_account_offers_a_merge_and_grants_nothing(db, bonus_on) -> None:
    """Слияние не молчаливое и бонус не раздаёт: провайдер остаётся у владельца, привязки нет вообще."""
    user, sub_id = await _paying_user(db)
    owner, _ = await _paying_user(db, telegram_id=7002, google_id='G1')
    before = await _end(db, sub_id)

    result = await _link(db, user, provider_id='G1', owner=owner)

    assert result.merge_required is True and result.merge_token == 'merge-token'
    assert await _end(db, sub_id) == before and await _bonus_rows(db) == []
    assert user.google_id is None
    bonus_on.assert_not_awaited()


@uses_db
async def test_a_provider_claimed_in_a_race_answers_409_and_grants_nothing(db, bonus_on) -> None:
    """Между проверкой и записью чужой аккаунт занял провайдер: UNIQUE даёт 409, бонус не выдаётся."""
    user, sub_id = await _paying_user(db)
    await _paying_user(db, telegram_id=7002, google_id='G1')  # занял; а get_user_by_oauth_provider этого не увидел
    before = await _end(db, sub_id)

    with pytest.raises(HTTPException) as refused:
        await _link(db, user, provider_id='G1', owner=None)

    assert refused.value.status_code == 409
    assert await _end(db, sub_id) == before and await _bonus_rows(db) == []
    bonus_on.assert_not_awaited()


@uses_db
async def test_a_bonus_failure_never_fails_the_link(db, monkeypatch, bonus_on) -> None:
    async def _boom(*args, **kwargs):
        raise RuntimeError('cleanup failed')

    monkeypatch.setattr(service, 'clear_notifications', _boom)
    user, sub_id = await _paying_user(db)
    user_id = user.id
    before = await _end(db, sub_id)

    result = await _link(db, user)

    assert (result.success, result.message) == (True, 'linked')
    assert (await db.execute(select(User.google_id).where(User.id == user_id))).scalar_one() == 'G1'
    assert await _end(db, sub_id) == before and await _bonus_rows(db) == []
    bonus_on.assert_not_awaited()


# ---------------------------------------------------------------------------
# Привязка при входе: провайдер цепляется к аккаунту с той же подтверждённой почтой
# ---------------------------------------------------------------------------


async def _login_callback(db, local_user, *, provider_id='G1'):
    info = SimpleNamespace(
        provider_id=provider_id,
        email=local_user.email,
        email_verified=True,
        first_name='A',
        last_name='B',
        username='a',
    )
    oauth_provider = MagicMock(
        exchange_code=AsyncMock(return_value={'access_token': 'tok'}), get_user_info=AsyncMock(return_value=info)
    )
    request = MagicMock(code='code', state='state', device_id=None, campaign_slug=None, referral_code=None)
    finalize = AsyncMock(return_value=MagicMock(name='AuthResponse'))
    patches = [
        patch.object(oauth_routes, 'validate_oauth_state', AsyncMock(return_value={'linking': 'false'})),
        patch.object(oauth_routes, 'get_provider', MagicMock(return_value=oauth_provider)),
        patch.object(oauth_routes, 'get_user_by_oauth_provider', AsyncMock(return_value=None)),
        patch.object(oauth_routes, 'get_user_by_email', AsyncMock(return_value=local_user)),
        patch.object(oauth_routes, '_gate_oauth_identity', AsyncMock()),
        patch.object(oauth_routes, '_finalize_oauth_login', finalize),
    ]
    for p in patches:
        p.start()
    try:
        await oauth_routes.oauth_callback('google', request, db)
    finally:
        for p in patches:
            p.stop()
    return finalize


@uses_db
async def test_signing_in_with_a_provider_that_links_by_verified_email_grants_the_bonus(db, bonus_on) -> None:
    user, sub_id = await _paying_user(db, email='a@example.com', email_verified=True, password_hash='x')
    before = await _end(db, sub_id)

    finalize = await _login_callback(db, user)

    finalize.assert_awaited_once()
    assert user.google_id == 'G1'
    assert await _end(db, sub_id) == before + timedelta(days=DAYS)
    (row,) = await _bonus_rows(db)
    assert (row.user_id, row.method) == (user.id, 'google')
    bonus_on.assert_awaited_once()


@uses_db
async def test_signing_in_again_with_the_linked_provider_grants_nothing_more(db, bonus_on) -> None:
    user, sub_id = await _paying_user(db, email='a@example.com', email_verified=True, password_hash='x')
    before = await _end(db, sub_id)

    await _login_callback(db, user, provider_id='G1')
    user.google_id = None  # отвязал и снова вошёл тем же способом
    await db.commit()
    await _login_callback(db, user, provider_id='G1')

    assert await _end(db, sub_id) == before + timedelta(days=DAYS)
    assert len(await _bonus_rows(db)) == 1


@uses_db
async def test_a_revived_deleted_account_gets_no_bonus(db, bonus_on) -> None:
    user, sub_id = await _paying_user(db, email='a@example.com', email_verified=True, password_hash='x')
    user.status = UserStatus.DELETED.value
    await db.commit()
    before = await _end(db, sub_id)

    await _login_callback(db, user)

    assert await _end(db, sub_id) == before and await _bonus_rows(db) == []
    bonus_on.assert_not_awaited()


@uses_db
async def test_the_login_flow_is_untouched_when_the_feature_is_off(db, monkeypatch, bonus_on) -> None:
    monkeypatch.setattr(settings, 'AUTH_LINK_BONUS_ENABLED', False)
    user, sub_id = await _paying_user(db, email='a@example.com', email_verified=True, password_hash='x')
    before = await _end(db, sub_id)

    finalize = await _login_callback(db, user)

    finalize.assert_awaited_once()
    assert user.google_id == 'G1'
    assert await _end(db, sub_id) == before and await _bonus_rows(db) == []
    bonus_on.assert_not_awaited()
