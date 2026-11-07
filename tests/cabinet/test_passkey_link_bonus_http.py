"""Бонус за привязку второго способа входа через настоящую ручку регистрации passkey.

Бонус выдаётся тем же коммитом, что и сам ключ; сбой бонуса ключ не ломает; создание аккаунта passkey-ом
бонуса не даёт никогда.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from app.cabinet.routes import passkey as passkey_routes
from app.config import settings
from app.database.models import UserPasskey
from app.services import auth_link_bonus_notifications as notifications, auth_link_bonus_service as service
from tests.cabinet.passkey_harness import new_authenticator, open_harness


pytestmark = [pytest.mark.usefixtures('passkey_settings'), pytest.mark.asyncio]

DAYS = 7


@pytest.fixture(autouse=True)
def bonus_on(monkeypatch) -> AsyncMock:
    monkeypatch.setattr(settings, 'AUTH_LINK_BONUS_ENABLED', True)
    monkeypatch.setattr(settings, 'AUTH_LINK_BONUS_DAYS', DAYS)
    monkeypatch.setattr(settings, 'AUTH_LINK_BONUS_REQUIRES_PAID', True)
    delivered = AsyncMock()
    monkeypatch.setattr(passkey_routes, 'deliver_link_bonus', delivered)
    return delivered


async def test_the_first_passkey_extends_the_paid_subscription_once(monkeypatch, fake_redis, bonus_on) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        user, sub_id = await h.make_paying_user(days=30)
        before = await h.subscription_end(sub_id)

        _, response = await h.register(user)

        assert response.status_code == 201, response.text
        assert await h.subscription_end(sub_id) == before + timedelta(days=DAYS)
        (row,) = await h.bonus_rows()
        assert (row.user_id, row.method) == (user.id, 'passkey')
        assert len(await h.passkeys(user)) == 1
    bonus_on.assert_awaited_once()
    grant = bonus_on.await_args.args[2]
    assert (grant.days, grant.method, grant.user_id) == (DAYS, 'passkey', user.id)


async def test_the_response_body_is_unchanged_by_the_bonus(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        user, _ = await h.make_paying_user()
        _, response = await h.register(user, name='Laptop')

    assert response.status_code == 201
    assert set(response.json()) == {
        'id',
        'name',
        'device_type',
        'backed_up',
        'transports',
        'created_at',
        'last_used_at',
    }


async def test_a_second_passkey_does_not_repeat_the_bonus(monkeypatch, fake_redis, bonus_on) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        user, sub_id = await h.make_paying_user(days=30)
        before = await h.subscription_end(sub_id)

        await h.register(user)
        _, second = await h.register(user)

        assert second.status_code == 201
        assert await h.subscription_end(sub_id) == before + timedelta(days=DAYS)
        assert len(await h.bonus_rows()) == 1
    bonus_on.assert_awaited_once()


async def test_deleting_and_registering_again_does_not_repeat_the_bonus(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        user, sub_id = await h.make_paying_user(days=30, email='a@example.com', password_hash='x')
        before = await h.subscription_end(sub_id)

        _, first = await h.register(user)
        deleted = await h.client.delete(f'/cabinet/passkey/{first.json()["id"]}', headers=h.auth(user))
        assert deleted.status_code == 200, deleted.text
        _, again = await h.register(user)

        assert again.status_code == 201
        assert await h.subscription_end(sub_id) == before + timedelta(days=DAYS)
        assert len(await h.bonus_rows()) == 1


async def test_an_account_with_an_oauth_provider_already_linked_gets_no_bonus(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        user, sub_id = await h.make_paying_user(days=30, google_id='sub-1')
        before = await h.subscription_end(sub_id)

        _, response = await h.register(user)

        assert response.status_code == 201
        assert await h.subscription_end(sub_id) == before
        assert await h.bonus_rows() == []


async def test_telegram_and_email_do_not_count_as_a_linked_method(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        user, sub_id = await h.make_paying_user(days=30, telegram_id=555001, email='a@example.com', password_hash='x')
        before = await h.subscription_end(sub_id)

        _, response = await h.register(user)

        assert response.status_code == 201
        assert await h.subscription_end(sub_id) == before + timedelta(days=DAYS)


async def test_the_feature_is_off_by_default(monkeypatch, fake_redis, bonus_on) -> None:
    monkeypatch.setattr(settings, 'AUTH_LINK_BONUS_ENABLED', False)
    async with open_harness(monkeypatch, fake_redis) as h:
        user, sub_id = await h.make_paying_user(days=30)
        before = await h.subscription_end(sub_id)

        _, response = await h.register(user)

        assert response.status_code == 201
        assert await h.subscription_end(sub_id) == before and await h.bonus_rows() == []
    bonus_on.assert_not_awaited()


async def test_an_account_without_payment_history_gets_no_bonus(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        user, sub_id = await h.make_paying_user(days=30, paid=False)
        before = await h.subscription_end(sub_id)

        _, response = await h.register(user)

        assert response.status_code == 201
        assert await h.subscription_end(sub_id) == before and await h.bonus_rows() == []


async def test_a_bonus_failure_never_fails_the_registration(monkeypatch, fake_redis, bonus_on) -> None:
    async def _boom(*args, **kwargs):
        raise RuntimeError('cleanup failed')

    monkeypatch.setattr(service, 'clear_notifications', _boom)
    async with open_harness(monkeypatch, fake_redis) as h:
        user, sub_id = await h.make_paying_user(days=30)
        before = await h.subscription_end(sub_id)

        _, response = await h.register(user)

        assert response.status_code == 201, response.text
        assert len(await h.passkeys(user)) == 1, 'ключ сохранён'
        assert await h.subscription_end(sub_id) == before, 'ни продления...'
        assert await h.bonus_rows() == [], '...ни полупримененной строки'
    bonus_on.assert_not_awaited()


async def test_a_failing_notification_never_fails_the_registration(monkeypatch, fake_redis) -> None:
    async def _down(*args, **kwargs):
        raise RuntimeError('telegram down')

    monkeypatch.setattr(passkey_routes, 'deliver_link_bonus', notifications.deliver_link_bonus)
    monkeypatch.setattr(notifications, '_sync_panel', _down)
    monkeypatch.setattr(notifications, '_notify_cabinet', _down)
    monkeypatch.setattr(notifications, '_notify_user', _down)
    async with open_harness(monkeypatch, fake_redis) as h:
        user, sub_id = await h.make_paying_user(days=30)
        before = await h.subscription_end(sub_id)

        _, response = await h.register(user)

        assert response.status_code == 201, response.text
        assert await h.subscription_end(sub_id) == before + timedelta(days=DAYS)


async def test_a_refused_registration_grants_nothing(monkeypatch, fake_redis, bonus_on) -> None:
    """Чужой ключ (credential_id уже занят) привязкой не считается: 409, продления нет."""
    async with open_harness(monkeypatch, fake_redis) as h:
        owner, _ = await h.make_paying_user(days=30)
        thief, thief_sub = await h.make_paying_user(days=30)
        thief_before = await h.subscription_end(thief_sub)
        authenticator = new_authenticator()
        await h.register(owner, authenticator)
        bonus_on.reset_mock()

        _, response = await h.register(thief, authenticator)

        assert response.status_code == 409, response.text
        assert await h.subscription_end(thief_sub) == thief_before
        assert [row.user_id for row in await h.bonus_rows()] == [owner.id]
    bonus_on.assert_not_awaited()


async def test_a_bad_attestation_grants_nothing(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        user, sub_id = await h.make_paying_user(days=30)
        before = await h.subscription_end(sub_id)

        response = await h.register_verify(user, 'c' * 32, {})

        assert response.status_code == 400
        assert await h.subscription_end(sub_id) == before and await h.bonus_rows() == []


@pytest.mark.usefixtures('passkey_signup_settings')
async def test_creating_an_account_with_a_passkey_never_grants_a_bonus(monkeypatch, fake_redis, bonus_on) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        _, response = await h.signup()

        assert response.status_code == 200, response.text
        assert await h.bonus_rows() == []
        assert len(await h.passkeys()) == 1
    bonus_on.assert_not_awaited()


async def test_the_unique_constraint_is_the_only_guard_in_the_route(monkeypatch, fake_redis) -> None:
    """Предпроверка «бонус уже получен» обойдена: повтор всё равно отсекает UNIQUE, ключ при этом сохраняется."""

    async def _not_yet(_db, _user_id) -> bool:
        return False

    monkeypatch.setattr(service, 'has_received_bonus', _not_yet)
    async with open_harness(monkeypatch, fake_redis) as h:
        user, sub_id = await h.make_paying_user(days=30)
        before = await h.subscription_end(sub_id)

        await h.register(user)
        _, second = await h.register(user)

        assert second.status_code == 201
        assert await h.subscription_end(sub_id) == before + timedelta(days=DAYS)
        assert len(await h.bonus_rows()) == 1
        assert len(await h.passkeys(user)) == 2


async def test_passkey_rows_are_kept_when_the_bonus_refuses(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        user, _ = await h.make_paying_user(days=30, paid=False)
        _, response = await h.register(user)
        rows = (await h.db.execute(UserPasskey.__table__.select())).all()

    assert response.status_code == 201 and len(rows) == 1
