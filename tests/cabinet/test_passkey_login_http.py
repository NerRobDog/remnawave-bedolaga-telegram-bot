"""Вход по passkey через HTTP: сессия, одноразовость, неразличимые отказы, rate-limit."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import update

from app.cabinet.auth import get_token_payload, passkey
from app.cabinet.routes import passkey as passkey_routes
from app.database.crud.user_passkey import record_assertion
from app.database.models import User, UserPasskey, UserStatus
from tests.cabinet.passkey_harness import new_authenticator, open_harness
from tests.fixtures.webauthn_authenticator import b64u


pytestmark = [pytest.mark.usefixtures('passkey_settings'), pytest.mark.asyncio]

GENERIC_REFUSAL = {'detail': 'Passkey authentication failed'}


async def _user_with_key(h, **user_fields):
    user = await h.make_user(**user_fields)
    authenticator, response = await h.register(user)
    assert response.status_code == 201, response.text
    return user, authenticator


# --- удачный вход ------------------------------------------------------------


async def test_login_options_need_no_session_and_expose_no_credential_list(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        response = await h.login_options()

    body = response.json()
    assert response.status_code == 200
    assert body['options']['rpId'] == 'watchd0g.dev'
    assert body['options']['userVerification'] == 'required'
    assert body['options'].get('allowCredentials', []) == []
    assert body['challenge_id']


async def test_login_issues_a_session_for_the_owner_of_the_key(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        user, authenticator = await _user_with_key(h, telegram_id=555, email='alice@example.org')
        # Регистрация ключа шла с JWT и уже поставила метку; сбрасываем, чтобы увидеть метку именно входа.
        await h.db.execute(update(User).where(User.id == user.id).values(cabinet_last_login=None))
        await h.db.commit()

        response = await h.login(authenticator)
        rows = await h.passkeys(user)
        tokens = await h.refresh_tokens()
        reloaded = await h.db.get(User, user.id, populate_existing=True)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body['token_type'] == 'bearer'
    assert body['user']['id'] == user.id and body['user']['email'] == 'alice@example.org'
    assert get_token_payload(body['access_token'], expected_type='access')['sub'] == str(user.id)
    assert get_token_payload(body['refresh_token'], expected_type='refresh')['sub'] == str(user.id)
    assert [token.user_id for token in tokens] == [user.id]
    assert reloaded.cabinet_last_login is not None
    assert (datetime.now(UTC) - reloaded.cabinet_last_login).total_seconds() < 60
    assert rows[0].sign_count == 1
    assert rows[0].last_used_at is not None


async def test_session_from_passkey_login_opens_the_cabinet(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        user, authenticator = await _user_with_key(h)
        token = (await h.login(authenticator)).json()['access_token']

        listing = await h.client.get('/cabinet/passkey', headers={'Authorization': f'Bearer {token}'})

    assert listing.status_code == 200
    assert len(listing.json()['passkeys']) == 1


async def test_device_without_a_counter_can_log_in_repeatedly(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        _, authenticator = await _user_with_key(h)

        first = await h.login(authenticator, sign_count=0)
        second = await h.login(authenticator, sign_count=0)

    assert first.status_code == 200 and second.status_code == 200


async def test_second_key_of_the_same_user_logs_in_to_the_same_account(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        user = await h.make_user()
        phone, _ = await h.register(user)
        laptop, _ = await h.register(user)

        via_phone = await h.login(phone)
        via_laptop = await h.login(laptop)

    assert via_phone.json()['user']['id'] == via_laptop.json()['user']['id'] == user.id


# --- челлендж ----------------------------------------------------------------


async def test_a_signed_assertion_cannot_be_replayed(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        _, authenticator = await _user_with_key(h)
        options = (await h.login_options()).json()
        assertion = authenticator.get(options['options'])

        first = await h.login_verify(options['challenge_id'], assertion)
        replay = await h.login_verify(options['challenge_id'], assertion)
        tokens = await h.refresh_tokens()

    assert first.status_code == 200
    assert replay.status_code == 401 and replay.json() == GENERIC_REFUSAL
    assert len(tokens) == 1


async def test_an_expired_challenge_cannot_log_in(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        _, authenticator = await _user_with_key(h)
        options = (await h.login_options()).json()
        assertion = authenticator.get(options['options'])

        fake_redis.advance(passkey.CHALLENGE_TTL_SECONDS + 1)
        response = await h.login_verify(options['challenge_id'], assertion)

    assert response.status_code == 401 and response.json() == GENERIC_REFUSAL


async def test_a_registration_challenge_cannot_log_in(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        user, authenticator = await _user_with_key(h)
        registration = (await h.register_options(user)).json()
        assertion = authenticator.get(registration['options'])

        response = await h.login_verify(registration['challenge_id'], assertion)

    assert response.status_code == 401 and response.json() == GENERIC_REFUSAL


async def test_a_failed_attempt_burns_the_challenge(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        _, authenticator = await _user_with_key(h)
        options = (await h.login_options()).json()

        bad = await h.login_verify(
            options['challenge_id'], authenticator.get(options['options'], origin='https://evil.example')
        )
        good = await h.login_verify(options['challenge_id'], authenticator.get(options['options']))

    assert bad.status_code == 401
    assert good.status_code == 401


# --- отказы неразличимы --------------------------------------------------------


@pytest.mark.parametrize(
    'status',
    [UserStatus.BLOCKED.value, UserStatus.DELETED.value],
)
async def test_blocked_or_deleted_account_gets_the_same_401_as_an_unknown_key(monkeypatch, fake_redis, status) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        user, authenticator = await _user_with_key(h)
        stranger = new_authenticator()
        stranger.user_handle = authenticator.user_handle
        unknown = await h.login(stranger)

        await h.db.execute(user.__table__.update().where(user.__table__.c.id == user.id).values(status=status))
        await h.db.commit()
        refused = await h.login(authenticator)
        tokens = await h.refresh_tokens()
        rows = await h.passkeys(user)

    assert refused.status_code == 401
    assert refused.json() == unknown.json() == GENERIC_REFUSAL
    assert dict(refused.headers).keys() >= {'content-type'}
    assert tokens == []
    assert rows[0].sign_count == 0 and rows[0].last_used_at is None


@pytest.mark.parametrize(
    ('label', 'get_kwargs'),
    [
        ('foreign-origin', {'origin': 'https://evil.example'}),
        ('foreign-rp-id', {'rp_id': 'evil.example'}),
        ('no-user-verification', {'user_verified': False}),
        ('wrong-challenge', {'challenge': b64u(b'q' * 32)}),
        ('wrong-type', {'client_data_type': 'webauthn.create'}),
    ],
)
async def test_unverifiable_assertions_get_the_generic_401(monkeypatch, fake_redis, label, get_kwargs) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        _, authenticator = await _user_with_key(h)
        response = await h.login(authenticator, **get_kwargs)
        tokens = await h.refresh_tokens()

    assert response.status_code == 401, label
    assert response.json() == GENERIC_REFUSAL
    assert tokens == []


async def test_signature_from_another_key_under_a_known_credential_id_is_refused(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        _, authenticator = await _user_with_key(h)
        response = await h.login(authenticator, signing_key=authenticator.foreign_key())

    assert response.status_code == 401 and response.json() == GENERIC_REFUSAL


async def test_assertion_claiming_another_accounts_user_handle_is_refused(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        _, authenticator = await _user_with_key(h)
        other = await h.make_user()
        response = await h.login(authenticator, user_handle=passkey.user_handle_for(other.id))

    assert response.status_code == 401 and response.json() == GENERIC_REFUSAL


async def test_authenticator_that_omits_user_handle_still_logs_in(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        _, authenticator = await _user_with_key(h)
        response = await h.login(authenticator, omit_user_handle=True)

    assert response.status_code == 200


@pytest.mark.parametrize('credential', [None, 'text', 7, {}, {'rawId': 'x'}, {'rawId': '***', 'response': {}}])
async def test_garbage_assertions_never_crash(monkeypatch, fake_redis, credential) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        options = (await h.login_options()).json()
        response = await h.post(
            '/cabinet/auth/passkey/login/verify', challenge_id=options['challenge_id'], credential=credential
        )

    assert response.status_code in (401, 422), response.text
    if response.status_code == 401:
        assert response.json() == GENERIC_REFUSAL


# --- счётчик подписей -----------------------------------------------------------


async def test_sign_count_that_does_not_advance_is_treated_as_a_cloned_key(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        user, authenticator = await _user_with_key(h)
        assert (await h.login(authenticator, sign_count=5)).status_code == 200

        same = await h.login(authenticator, sign_count=5)
        rollback = await h.login(authenticator, sign_count=3)
        forward = await h.login(authenticator, sign_count=6)
        rows = await h.passkeys(user)

    assert same.status_code == 401 and same.json() == GENERIC_REFUSAL
    assert rollback.status_code == 401
    assert forward.status_code == 200
    assert rows[0].sign_count == 6


async def test_counterless_key_that_suddenly_counts_is_still_accepted_forward(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        _, authenticator = await _user_with_key(h)
        assert (await h.login(authenticator, sign_count=0)).status_code == 200

        counting = await h.login(authenticator, sign_count=1)

    assert counting.status_code == 200


async def test_concurrent_login_with_a_stale_counter_loses(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        user, authenticator = await _user_with_key(h)
        stored = (await h.passkeys(user))[0]

        first = await record_assertion(h.db, stored, seen_sign_count=0, new_sign_count=1, backed_up=False)
        second = await record_assertion(h.db, stored, seen_sign_count=0, new_sign_count=1, backed_up=False)
        await h.db.commit()
        rows = await h.passkeys(user)

    assert first is True
    assert second is False
    assert rows[0].sign_count == 1
    assert authenticator.credential_id == rows[0].credential_id


async def test_login_that_loses_the_counter_race_is_refused_and_keeps_the_winners_counter(
    monkeypatch, fake_redis
) -> None:
    """Между проверкой подписи и записью тот же ключ уже вошёл параллельным запросом."""
    async with open_harness(monkeypatch, fake_redis) as h:
        user, authenticator = await _user_with_key(h)
        real_get_user = passkey_routes.get_user_by_id

        async def get_user_after_a_concurrent_login(db, user_id):
            await db.execute(update(UserPasskey).where(UserPasskey.user_id == user_id).values(sign_count=7))
            await db.commit()
            return await real_get_user(db, user_id)

        monkeypatch.setattr(passkey_routes, 'get_user_by_id', get_user_after_a_concurrent_login)
        response = await h.login(authenticator)
        rows = await h.passkeys(user)
        tokens = await h.refresh_tokens()

    assert response.status_code == 401 and response.json() == GENERIC_REFUSAL
    assert rows[0].sign_count == 7
    assert tokens == []


# --- rate-limit ----------------------------------------------------------------


async def test_login_options_are_rate_limited_per_ip(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        statuses = [(await h.login_options()).status_code for _ in range(21)]

    assert statuses[:20] == [200] * 20
    assert statuses[20] == 429


async def test_login_verify_is_rate_limited_per_ip(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        statuses = [(await h.login_verify('c' * 32, {})).status_code for _ in range(11)]

    assert statuses[:10] == [401] * 10
    assert statuses[10] == 429


async def test_login_fails_closed_when_the_limiter_cannot_reach_redis(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        monkeypatch.setattr(passkey.cache, '_connected', False)
        options = await h.login_options()
        verify = await h.login_verify('c' * 32, {})

    assert options.status_code == 429
    assert verify.status_code == 429


async def test_challenge_store_outage_after_the_limiter_is_a_503(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:

        async def not_limited(*_args, **_kwargs):
            return False

        monkeypatch.setattr(passkey_routes.RateLimitCache, 'is_ip_rate_limited', not_limited)
        monkeypatch.setattr(passkey.cache, '_connected', False)
        response = await h.login_options()

    assert response.status_code == 503
    assert response.json()['detail']['code'] == 'passkey_unavailable'


# --- аккаунт, созданный самим passkey: handle случайный и хранится у ключа -------


@pytest.mark.usefixtures('passkey_signup_settings')
async def test_a_key_from_signup_logs_in_with_its_stored_random_handle(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        authenticator, signed_up = await h.signup()
        response = await h.login(authenticator)

    assert response.status_code == 200, response.text
    assert response.json()['user']['id'] == signed_up.json()['user']['id']
    assert authenticator.user_handle != passkey.user_handle_for(signed_up.json()['user']['id'])


@pytest.mark.usefixtures('passkey_signup_settings')
async def test_a_key_from_signup_also_accepts_the_derived_handle_and_no_handle(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        authenticator, signed_up = await h.signup()
        derived = passkey.user_handle_for(signed_up.json()['user']['id'])
        with_derived = await h.login(authenticator, user_handle=derived)
        without = await h.login(authenticator, omit_user_handle=True)

    assert with_derived.status_code == 200 and without.status_code == 200


@pytest.mark.usefixtures('passkey_signup_settings')
async def test_a_signup_key_that_presents_another_accounts_handle_is_refused(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        mine, _ = await h.signup()
        other, other_account = await h.signup()
        stranger = await h.make_user()

        stolen = [
            other.user_handle,  # случайный handle чужого passkey-аккаунта
            passkey.user_handle_for(other_account.json()['user']['id']),
            passkey.user_handle_for(stranger.id),
            b'\x00' * 32,
        ]
        responses = [await h.login(mine, user_handle=handle) for handle in stolen]

    for response in responses:
        assert response.status_code == 401 and response.json() == GENERIC_REFUSAL


@pytest.mark.usefixtures('passkey_signup_settings')
async def test_a_legacy_key_without_a_stored_handle_still_needs_the_derived_one(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        user, authenticator = await _user_with_key(h)
        (key,) = await h.passkeys(user)
        derived = await h.login(authenticator)
        random_handle = await h.login(authenticator, user_handle=b'\x07' * 32)

    assert key.user_handle is None
    assert derived.status_code == 200
    assert random_handle.status_code == 401
