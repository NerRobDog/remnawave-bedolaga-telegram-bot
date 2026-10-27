"""Регистрация аккаунта одним passkey через HTTP: гейты, лимиты, атомарность, никаких бонусов."""

from __future__ import annotations

import re
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from structlog.testing import capture_logs

from app.cabinet.auth import get_token_payload, passkey
from app.cabinet.routes import passkey_signup as signup_routes
from app.config import settings
from app.database.models import LegalConsent, Subscription
from tests.cabinet.passkey_harness import new_authenticator, open_harness


pytestmark = [pytest.mark.usefixtures('passkey_settings'), pytest.mark.asyncio]

OPTIONS = '/cabinet/auth/passkey/signup/options'
SHOWN_CODE = re.compile(r'[0-9A-Z]{4}(-[0-9A-Z]{4}){3}')


@pytest.fixture(autouse=True)
def signup_on(passkey_signup_settings) -> AsyncMock:
    return passkey_signup_settings


# --- гейты -------------------------------------------------------------------


async def test_status_tells_the_frontend_whether_signup_is_open(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        opened = await h.client.get('/cabinet/auth/passkey/status')
        monkeypatch.setattr(settings, 'PASSKEY_SIGNUP_ENABLED', False)
        closed = await h.client.get('/cabinet/auth/passkey/status')

    assert opened.json() == {'enabled': True, 'signup_enabled': True}
    assert closed.json() == {'enabled': True, 'signup_enabled': False}


@pytest.mark.parametrize(
    'overrides',
    [{'PASSKEY_SIGNUP_ENABLED': False}, {'PASSKEY_ENABLED': False}, {'PASSKEY_RP_ID': ''}],
    ids=['signup-off-by-default', 'passkey-off', 'no-rp-id'],
)
async def test_signup_endpoints_answer_disabled_when_closed(monkeypatch, fake_redis, overrides) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        for name, value in overrides.items():
            monkeypatch.setattr(settings, name, value)

        options = await h.signup_options()
        verify = await h.signup_verify('c' * 32, {})
        users = await h.users()

    for response in (options, verify):
        assert response.status_code == 404, response.text
        assert response.json()['detail']['code'] == 'passkey_signup_disabled'
    assert users == []


async def test_invite_only_mode_closes_passkey_signup(monkeypatch, fake_redis) -> None:
    """У passkey-регистрации нет личности, по которой можно проверить приглашение: пускать нельзя."""
    monkeypatch.setattr(settings, 'INVITE_ONLY_ENABLED', True)
    async with open_harness(monkeypatch, fake_redis) as h:
        options = await h.signup_options()
        verify = await h.signup_verify('c' * 32, {})
        users = await h.users()

    for response in (options, verify):
        assert response.status_code == 403
        assert response.json()['detail'] == {'code': 'registration_invite_required'}
    assert users == []


# --- options -----------------------------------------------------------------


async def test_options_accept_an_empty_body_and_a_display_name(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        bare = await h.client.post(OPTIONS)
        empty = await h.signup_options()
        named = await h.signup_options(display_name='  Alice  ')

    assert bare.status_code == empty.status_code == named.status_code == 200
    assert named.json()['options']['user']['displayName'] == 'Alice'
    assert bare.json()['options']['authenticatorSelection']['residentKey'] == 'required'
    assert bare.json()['options']['user']['id'] != empty.json()['options']['user']['id']


@pytest.mark.parametrize('name', ['', '   ', 'x' * 101])
async def test_options_reject_a_blank_or_overlong_display_name(monkeypatch, fake_redis, name) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        response = await h.signup_options(display_name=name)

    assert response.status_code == 422


async def test_options_are_rate_limited_per_ip(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        statuses = [(await h.signup_options()).status_code for _ in range(22)]

    assert statuses[:20] == [200] * 20 and statuses[20:] == [429, 429]


async def test_options_answer_503_when_the_challenge_cannot_be_stored(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        monkeypatch.setattr(passkey.cache, 'set', AsyncMock(return_value=False))
        response = await h.signup_options()

    assert response.status_code == 503
    assert response.json()['detail']['code'] == 'passkey_unavailable'


async def test_options_fail_closed_when_the_limiter_has_no_redis(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        monkeypatch.setattr(passkey.cache, '_connected', False)
        response = await h.signup_options()

    assert response.status_code == 429


# --- удачная регистрация -------------------------------------------------------


async def test_signup_creates_an_account_a_key_and_eight_codes(monkeypatch, fake_redis, signup_on) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        authenticator, response = await h.signup(display_name='Alice')
        users = await h.users()
        keys = await h.passkeys()
        codes = await h.recovery_rows()
        tokens = await h.refresh_tokens()
        subscriptions = (await h.db.execute(select(Subscription))).scalars().all()

    assert response.status_code == 200, response.text
    body = response.json()
    (user,) = users
    assert (user.auth_type, user.telegram_id, user.email, user.password_hash) == ('passkey', None, None, None)
    assert user.status == 'active' and user.first_name == 'Alice' and user.balance_kopeks == 0
    assert user.referral_code and user.promo_group_id is not None and user.referred_by_id is None
    assert body['user']['id'] == user.id and body['user']['auth_type'] == 'passkey'
    assert get_token_payload(body['access_token'], expected_type='access')['sub'] == str(user.id)
    assert get_token_payload(body['refresh_token'], expected_type='refresh')['sub'] == str(user.id)
    assert [token.user_id for token in tokens] == [user.id]
    (key,) = keys
    assert key.user_id == user.id and key.credential_id == authenticator.credential_id
    assert key.user_handle == authenticator.user_handle and len(key.user_handle) == 32
    assert key.name == 'Passkey 1'
    assert subscriptions == [], 'ни триала, ни подписки при регистрации'
    assert body.get('campaign_bonus') is None
    assert len(body['recovery_codes']) == len(codes) == 8
    assert all(SHOWN_CODE.fullmatch(code) for code in body['recovery_codes'])
    assert {row.user_id for row in codes} == {user.id} and all(row.used_at is None for row in codes)
    signup_on.assert_awaited_once()
    assert signup_on.await_args.args[0] == 'user.created'
    assert signup_on.await_args.args[1]['auth_type'] == 'passkey' and signup_on.await_args.args[1]['user_id'] == user.id


async def test_the_key_can_be_named_at_signup(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        _, response = await h.signup(name='MacBook')
        (key,) = await h.passkeys()

    assert response.status_code == 200 and key.name == 'MacBook'


async def test_the_session_from_signup_opens_the_cabinet(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        _, response = await h.signup()
        token = response.json()['access_token']
        listing = await h.client.get('/cabinet/passkey', headers={'Authorization': f'Bearer {token}'})

    assert listing.status_code == 200 and len(listing.json()['passkeys']) == 1


async def test_recovery_codes_are_stored_only_as_hashes_and_never_logged(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        with capture_logs() as logs:
            _, response = await h.signup()
        stored = [row.code_hash for row in await h.recovery_rows()]

    shown = response.json()['recovery_codes']
    compact = [code.replace('-', '') for code in shown]
    assert not set(stored) & set(shown) and not set(stored) & set(compact)
    assert all(re.fullmatch(r'[0-9a-f]{64}', value) for value in stored)
    haystack = repr(logs).upper()
    assert not any(code in haystack or code.replace('-', '') in haystack for code in shown)


# --- бонусы и триал ------------------------------------------------------------


async def test_campaign_and_referral_fields_are_ignored_while_bonuses_are_off(monkeypatch, fake_redis) -> None:
    campaign = AsyncMock(return_value=None)
    referral = AsyncMock()
    monkeypatch.setattr(signup_routes, '_process_campaign_bonus', campaign)
    monkeypatch.setattr(signup_routes, '_process_referral_code', referral)
    async with open_harness(monkeypatch, fake_redis) as h:
        referrer = await h.make_user(referral_code='REFERRER1')
        _, response = await h.signup(campaign_slug='summer', referral_code='REFERRER1')
        new_user = (await h.users())[-1]

    assert response.status_code == 200
    campaign.assert_not_awaited()
    referral.assert_not_awaited()
    assert new_user.referred_by_id is None and new_user.id != referrer.id


async def test_campaign_and_referral_apply_only_when_the_bonus_flag_is_on(monkeypatch, fake_redis) -> None:
    monkeypatch.setattr(settings, 'PASSKEY_SIGNUP_BONUSES_ENABLED', True)
    campaign = AsyncMock(return_value=None)
    referral = AsyncMock()
    monkeypatch.setattr(signup_routes, '_process_campaign_bonus', campaign)
    monkeypatch.setattr(signup_routes, '_process_referral_code', referral)
    async with open_harness(monkeypatch, fake_redis) as h:
        _, response = await h.signup(campaign_slug='summer', referral_code='REFERRER1')
        new_user = (await h.users())[-1]

    assert response.status_code == 200
    assert campaign.await_args.args[2] == 'summer'
    assert referral.await_args.args[2] == 'REFERRER1' and referral.await_args.kwargs == {'is_new_user': True}
    assert referral.await_args.args[1].id == new_user.id


@pytest.mark.parametrize(
    ('trial_flag', 'disabled_for', 'expect_disabled'),
    [(False, 'none', True), (False, 'telegram', True), (True, 'none', False)],
)
async def test_a_signed_up_account_is_refused_a_trial_by_default(
    monkeypatch, fake_redis, trial_flag, disabled_for, expect_disabled
) -> None:
    monkeypatch.setattr(settings, 'TRIAL_DISABLED_FOR', disabled_for)
    monkeypatch.setattr(settings, 'PASSKEY_SIGNUP_TRIAL_ENABLED', trial_flag)
    async with open_harness(monkeypatch, fake_redis) as h:
        _, response = await h.signup()
        auth_type = response.json()['user']['auth_type']

    assert settings.is_trial_disabled_for_user(auth_type) is expect_disabled


# --- отказы и атомарность -----------------------------------------------------


async def test_a_bad_attestation_creates_nothing(monkeypatch, fake_redis, signup_on) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        options = (await h.signup_options()).json()
        credential = new_authenticator().create(options['options'], origin='https://evil.example')
        response = await h.signup_verify(options['challenge_id'], credential)
        users, keys, codes = await h.users(), await h.passkeys(), await h.recovery_rows()

    assert response.status_code == 400
    assert response.json()['detail']['code'] == 'verification_failed'
    assert users == keys == codes == []
    signup_on.assert_not_awaited()


async def test_the_challenge_is_spent_by_a_failed_attempt_and_by_a_replay(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        options = (await h.signup_options()).json()
        authenticator = new_authenticator()
        credential = authenticator.create(options['options'])
        bad = await h.signup_verify(options['challenge_id'], {**credential, 'rawId': 'AAAA'})
        retry = await h.signup_verify(options['challenge_id'], credential)
        users = await h.users()

    assert bad.status_code == 400
    assert retry.status_code == 400 and retry.json()['detail']['code'] == 'invalid_challenge'
    assert users == []


async def test_a_replayed_signup_response_does_not_create_a_second_account(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        options = (await h.signup_options()).json()
        credential = new_authenticator().create(options['options'])
        first = await h.signup_verify(options['challenge_id'], credential)
        second = await h.signup_verify(options['challenge_id'], credential)
        users = await h.users()

    assert first.status_code == 200 and second.status_code == 400
    assert len(users) == 1


async def test_a_challenge_issued_for_another_ceremony_cannot_create_an_account(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        owner = await h.make_user()
        register = (await h.register_options(owner)).json()
        login = (await h.login_options()).json()
        shape = (await h.signup_options()).json()['options']  # user.id и параметры для create()
        results = []
        for issued in (register, login):
            # Ответ подписан именно этим челленджем: отказ обязан быть из-за назначения, не из-за подписи.
            credential = new_authenticator().create(shape, challenge=issued['options']['challenge'])
            results.append(await h.signup_verify(issued['challenge_id'], credential))
        users = await h.users()

    assert [r.status_code for r in results] == [400, 400]
    assert [r.json()['detail']['code'] for r in results] == ['invalid_challenge', 'invalid_challenge']
    assert [u.id for u in users] == [owner.id]


async def test_an_expired_challenge_cannot_create_an_account(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        options = (await h.signup_options()).json()
        credential = new_authenticator().create(options['options'])
        fake_redis.advance(passkey.CHALLENGE_TTL_SECONDS + 1)
        response = await h.signup_verify(options['challenge_id'], credential)
        users = await h.users()

    assert response.status_code == 400 and users == []


async def test_a_signup_challenge_cannot_add_a_key_to_a_logged_in_user(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        owner = await h.make_user()
        options = (await h.signup_options()).json()
        credential = new_authenticator().create(options['options'])
        response = await h.register_verify(owner, options['challenge_id'], credential)
        keys = await h.passkeys()

    assert response.status_code == 400 and keys == []


async def test_a_credential_that_is_already_registered_answers_409_and_creates_no_account(
    monkeypatch, fake_redis, signup_on
) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        owner = await h.make_user()
        authenticator, registered = await h.register(owner)
        _, response = await h.signup(authenticator)
        users, keys, codes = await h.users(), await h.passkeys(), await h.recovery_rows()

    assert registered.status_code == 201
    assert response.status_code == 409
    assert response.json()['detail']['code'] == 'passkey_already_registered'
    assert [u.id for u in users] == [owner.id] and len(keys) == 1 and codes == []
    signup_on.assert_not_awaited()


async def test_the_same_authenticator_cannot_sign_up_twice(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        authenticator, first = await h.signup()
        _, second = await h.signup(authenticator)
        users = await h.users()

    assert first.status_code == 200 and second.status_code == 409
    assert len(users) == 1


async def test_a_failure_while_issuing_codes_rolls_the_whole_signup_back(monkeypatch, fake_redis, signup_on) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:

        async def boom(*args, **kwargs):
            raise RuntimeError('db went away')

        monkeypatch.setattr(signup_routes, 'replace_recovery_codes', boom)
        options = (await h.signup_options()).json()
        credential = new_authenticator().create(options['options'])
        with pytest.raises(RuntimeError):
            await h.signup_verify(options['challenge_id'], credential)
        users, keys = await h.users(), await h.passkeys()

    assert users == [] and keys == []
    signup_on.assert_not_awaited()


# --- лимит по IP -----------------------------------------------------------------


async def test_signup_attempts_are_limited_to_three_a_minute_and_failures_count(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        results = [await h.signup_verify('c' * 32, {}) for _ in range(3)]
        _, fourth = await h.signup()

    assert [r.status_code for r in results] == [400, 400, 400]
    assert fourth.status_code == 429
    assert fourth.headers['Retry-After'] == '60'


@pytest.mark.parametrize(
    ('limits', 'allowed', 'retry_after'),
    [
        ({'PASSKEY_SIGNUP_LIMIT_PER_MINUTE': 0, 'PASSKEY_SIGNUP_LIMIT_PER_HOUR': 2}, 2, '3600'),
        (
            {
                'PASSKEY_SIGNUP_LIMIT_PER_MINUTE': 0,
                'PASSKEY_SIGNUP_LIMIT_PER_HOUR': 0,
                'PASSKEY_SIGNUP_LIMIT_PER_DAY': 1,
            },
            1,
            '86400',
        ),
    ],
    ids=['hour-window', 'day-window'],
)
async def test_longer_windows_stop_a_slow_script(monkeypatch, fake_redis, limits, allowed, retry_after) -> None:
    for name, value in limits.items():
        monkeypatch.setattr(settings, name, value)
    async with open_harness(monkeypatch, fake_redis) as h:
        results = [await h.signup_verify('c' * 32, {}) for _ in range(allowed + 1)]

    assert [r.status_code for r in results] == [400] * allowed + [429]
    assert results[-1].headers['Retry-After'] == retry_after


async def test_zero_switches_every_window_off(monkeypatch, fake_redis) -> None:
    for name in ('PASSKEY_SIGNUP_LIMIT_PER_MINUTE', 'PASSKEY_SIGNUP_LIMIT_PER_HOUR', 'PASSKEY_SIGNUP_LIMIT_PER_DAY'):
        monkeypatch.setattr(settings, name, 0)
    async with open_harness(monkeypatch, fake_redis) as h:
        results = [await h.signup_verify('c' * 32, {}) for _ in range(40)]

    assert {r.status_code for r in results} == {400}


async def test_the_limiter_fails_closed_when_redis_is_down(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        options = (await h.signup_options()).json()
        credential = new_authenticator().create(options['options'])
        monkeypatch.setattr(passkey.cache, '_connected', False)
        response = await h.signup_verify(options['challenge_id'], credential)
        monkeypatch.setattr(passkey.cache, '_connected', True)
        users = await h.users()

    assert response.status_code == 429
    assert users == []


# --- согласие с документами --------------------------------------------------------


async def test_missing_legal_consent_answers_428_without_burning_the_challenge(monkeypatch, fake_redis) -> None:
    from app.services import legal_consent_service

    requirement = legal_consent_service.LegalConsentRequirement(
        required=True, prechecked=False, documents=['terms', 'privacy']
    )
    monkeypatch.setattr(legal_consent_service, 'get_requirement', AsyncMock(return_value=requirement))
    async with open_harness(monkeypatch, fake_redis) as h:
        options = (await h.signup_options()).json()
        credential = new_authenticator().create(options['options'])
        refused = await h.signup_verify(options['challenge_id'], credential, accepted_legal_documents=['terms'])
        users_after_refusal = await h.users()
        accepted = await h.signup_verify(
            options['challenge_id'], credential, accepted_legal_documents=['terms', 'privacy']
        )
        consents = (await h.db.execute(select(LegalConsent))).scalars().all()

    assert refused.status_code == 428
    assert refused.json()['detail']['missing'] == ['privacy']
    assert users_after_refusal == []
    assert accepted.status_code == 200, accepted.text
    assert sorted(c.document for c in consents) == ['privacy', 'terms']
    assert {c.source for c in consents} == {'cabinet_passkey'}


# --- новый аккаунт входит по своему ключу -------------------------------------------


async def test_the_new_account_logs_in_with_the_random_handle_the_key_was_created_with(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        authenticator, signed_up = await h.signup()
        logged_in = await h.login(authenticator)

    assert logged_in.status_code == 200, logged_in.text
    assert logged_in.json()['user']['id'] == signed_up.json()['user']['id']
    assert authenticator.user_handle is not None and len(authenticator.user_handle) == 32
