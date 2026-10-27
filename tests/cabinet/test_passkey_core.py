"""Ядро passkey: options → verify на настоящем py_webauthn и программном аутентификаторе.

Аутентификатор собирает clientDataJSON, authenticatorData и подпись вручную
(tests/fixtures/webauthn_authenticator.py); библиотека проверки не подменяется.
Каждый «отрицательный» тест ломает ровно одно условие, остальное остаётся верным —
иначе тест прошёл бы по другой причине, чем заявлена.
"""

from __future__ import annotations

import pytest

from app.cabinet.auth import passkey
from app.database.models import UserPasskey
from tests.cabinet.conftest import PASSKEY_ORIGIN, PASSKEY_RP_ID
from tests.fixtures.webauthn_authenticator import SoftwareAuthenticator, b64u, unb64u


pytestmark = [pytest.mark.usefixtures('passkey_settings'), pytest.mark.asyncio]

USER_ID = 7


def _authenticator(**kwargs) -> SoftwareAuthenticator:
    return SoftwareAuthenticator(rp_id=PASSKEY_RP_ID, origin=PASSKEY_ORIGIN, **kwargs)


async def _register(authenticator: SoftwareAuthenticator, *, user_id: int = USER_ID, **create_kwargs):
    """Полный круг регистрации; возвращает (challenge_id, options, credential)."""
    challenge_id, options = await passkey.create_registration_options(
        user_id=user_id, user_name='alice@example.org', display_name='Alice', existing=[]
    )
    credential = authenticator.create(options, **create_kwargs)
    return challenge_id, options, credential


async def _verified_registration(authenticator: SoftwareAuthenticator):
    challenge_id, _, credential = await _register(authenticator)
    challenge = await passkey.consume_challenge(challenge_id, purpose=passkey.PURPOSE_REGISTER, user_id=USER_ID)
    return passkey.verify_registration(challenge=challenge, credential=credential)


# --- опции регистрации -------------------------------------------------------


async def test_registration_options_require_user_verification_and_ask_no_attestation(fake_redis) -> None:
    _, options = await passkey.create_registration_options(
        user_id=USER_ID, user_name='alice@example.org', display_name='Alice', existing=[]
    )

    assert options['rp'] == {'id': PASSKEY_RP_ID, 'name': 'Watchdog'}
    assert options['attestation'] == 'none'
    assert options['authenticatorSelection']['userVerification'] == 'required'
    assert options['authenticatorSelection']['residentKey'] == 'required'
    assert options['user']['name'] == 'alice@example.org'
    assert options['user']['displayName'] == 'Alice'


async def test_registration_options_exclude_existing_credentials(fake_redis) -> None:
    known = UserPasskey(credential_id=b'\x01' * 32, transports='internal,hybrid')
    plain = UserPasskey(credential_id=b'\x02' * 32, transports=None)

    _, options = await passkey.create_registration_options(
        user_id=USER_ID, user_name='alice', display_name=None, existing=[known, plain]
    )

    excluded = {item['id']: item for item in options['excludeCredentials']}
    assert set(excluded) == {b64u(b'\x01' * 32), b64u(b'\x02' * 32)}
    assert excluded[b64u(b'\x01' * 32)]['transports'] == ['internal', 'hybrid']


async def test_user_handle_is_stable_opaque_and_not_the_numeric_id(fake_redis) -> None:
    _, first = await passkey.create_registration_options(user_id=USER_ID, user_name='a', display_name=None, existing=[])
    _, second = await passkey.create_registration_options(
        user_id=USER_ID, user_name='a', display_name=None, existing=[]
    )
    _, other = await passkey.create_registration_options(user_id=8, user_name='a', display_name=None, existing=[])

    assert first['user']['id'] == second['user']['id'] == b64u(passkey.user_handle_for(USER_ID))
    assert first['user']['id'] != other['user']['id']
    assert unb64u(first['user']['id']) != str(USER_ID).encode()
    assert len(unb64u(first['user']['id'])) == 32


# --- челлендж ----------------------------------------------------------------


async def test_challenge_lives_in_redis_for_300_seconds_under_its_own_key(fake_redis) -> None:
    challenge_id, _ = await passkey.create_login_options()

    key = f'passkey:chal:{challenge_id}'
    assert fake_redis.keys_with_prefix('passkey:chal:') == [key]
    assert fake_redis.ttl(key) == 300
    assert len(challenge_id) >= 32


async def test_each_options_call_issues_a_fresh_challenge(fake_redis) -> None:
    first_id, first = await passkey.create_login_options()
    second_id, second = await passkey.create_login_options()

    assert first_id != second_id
    assert first['challenge'] != second['challenge']


async def test_challenge_is_single_use(fake_redis) -> None:
    challenge_id, _ = await passkey.create_login_options()

    await passkey.consume_challenge(challenge_id, purpose=passkey.PURPOSE_LOGIN)
    with pytest.raises(passkey.PasskeyChallengeError):
        await passkey.consume_challenge(challenge_id, purpose=passkey.PURPOSE_LOGIN)


async def test_expired_challenge_is_refused(fake_redis) -> None:
    challenge_id, _ = await passkey.create_login_options()

    fake_redis.advance(301)

    with pytest.raises(passkey.PasskeyChallengeError):
        await passkey.consume_challenge(challenge_id, purpose=passkey.PURPOSE_LOGIN)


async def test_unknown_challenge_is_refused(fake_redis) -> None:
    with pytest.raises(passkey.PasskeyChallengeError):
        await passkey.consume_challenge('no-such-challenge', purpose=passkey.PURPOSE_LOGIN)


async def test_registration_challenge_belongs_to_the_user_who_asked_for_it(fake_redis) -> None:
    challenge_id, _, _ = await _register(_authenticator())

    with pytest.raises(passkey.PasskeyChallengeError):
        await passkey.consume_challenge(challenge_id, purpose=passkey.PURPOSE_REGISTER, user_id=USER_ID + 1)

    # Попытка с чужим user_id сжигает челлендж: подбирать его повторно нельзя.
    with pytest.raises(passkey.PasskeyChallengeError):
        await passkey.consume_challenge(challenge_id, purpose=passkey.PURPOSE_REGISTER, user_id=USER_ID)


async def test_login_challenge_cannot_be_spent_on_registration(fake_redis) -> None:
    challenge_id, _ = await passkey.create_login_options()

    with pytest.raises(passkey.PasskeyChallengeError):
        await passkey.consume_challenge(challenge_id, purpose=passkey.PURPOSE_REGISTER, user_id=USER_ID)


async def test_registration_challenge_cannot_be_spent_on_login(fake_redis) -> None:
    challenge_id, _, _ = await _register(_authenticator())

    with pytest.raises(passkey.PasskeyChallengeError):
        await passkey.consume_challenge(challenge_id, purpose=passkey.PURPOSE_LOGIN)


async def test_unavailable_redis_means_no_options_instead_of_unverifiable_ones(fake_redis, monkeypatch) -> None:
    monkeypatch.setattr(passkey.cache, '_connected', False)

    with pytest.raises(passkey.PasskeyUnavailableError):
        await passkey.create_login_options()


# --- проверка регистрации ----------------------------------------------------


async def test_registration_verifies_a_real_attestation(fake_redis) -> None:
    authenticator = _authenticator(backed_up=True)

    verified = await _verified_registration(authenticator)

    assert verified.credential_id == authenticator.credential_id
    assert verified.sign_count == 0
    assert verified.device_type == 'multi_device'
    assert verified.backed_up is True
    assert verified.transports == 'internal'
    assert verified.public_key  # COSE-ключ, без приватной части


async def test_registration_rejects_a_foreign_origin(fake_redis) -> None:
    challenge_id, _, credential = await _register(_authenticator(), origin='https://evil.example')
    challenge = await passkey.consume_challenge(challenge_id, purpose=passkey.PURPOSE_REGISTER, user_id=USER_ID)

    with pytest.raises(passkey.PasskeyVerificationError):
        passkey.verify_registration(challenge=challenge, credential=credential)


async def test_registration_rejects_a_foreign_rp_id(fake_redis) -> None:
    challenge_id, _, credential = await _register(_authenticator(), rp_id='evil.example')
    challenge = await passkey.consume_challenge(challenge_id, purpose=passkey.PURPOSE_REGISTER, user_id=USER_ID)

    with pytest.raises(passkey.PasskeyVerificationError):
        passkey.verify_registration(challenge=challenge, credential=credential)


async def test_registration_rejects_when_user_was_not_verified(fake_redis) -> None:
    challenge_id, _, credential = await _register(_authenticator(), user_verified=False)
    challenge = await passkey.consume_challenge(challenge_id, purpose=passkey.PURPOSE_REGISTER, user_id=USER_ID)

    with pytest.raises(passkey.PasskeyVerificationError):
        passkey.verify_registration(challenge=challenge, credential=credential)


async def test_registration_rejects_a_challenge_the_server_did_not_issue(fake_redis) -> None:
    challenge_id, _, credential = await _register(_authenticator(), challenge=b64u(b'x' * 32))
    challenge = await passkey.consume_challenge(challenge_id, purpose=passkey.PURPOSE_REGISTER, user_id=USER_ID)

    with pytest.raises(passkey.PasskeyVerificationError):
        passkey.verify_registration(challenge=challenge, credential=credential)


async def test_registration_rejects_an_assertion_type_in_client_data(fake_redis) -> None:
    challenge_id, _, credential = await _register(_authenticator(), client_data_type='webauthn.get')
    challenge = await passkey.consume_challenge(challenge_id, purpose=passkey.PURPOSE_REGISTER, user_id=USER_ID)

    with pytest.raises(passkey.PasskeyVerificationError):
        passkey.verify_registration(challenge=challenge, credential=credential)


async def test_registration_rejects_cross_origin_client_data(fake_redis) -> None:
    challenge_id, _, credential = await _register(_authenticator(), cross_origin=True)
    challenge = await passkey.consume_challenge(challenge_id, purpose=passkey.PURPOSE_REGISTER, user_id=USER_ID)

    with pytest.raises(passkey.PasskeyVerificationError):
        passkey.verify_registration(challenge=challenge, credential=credential)


async def test_registration_rejects_an_id_that_differs_from_the_attested_credential(fake_redis) -> None:
    challenge_id, _, credential = await _register(_authenticator(), raw_id=b'\x09' * 32)
    challenge = await passkey.consume_challenge(challenge_id, purpose=passkey.PURPOSE_REGISTER, user_id=USER_ID)

    with pytest.raises(passkey.PasskeyVerificationError):
        passkey.verify_registration(challenge=challenge, credential=credential)


@pytest.mark.parametrize('garbage', [None, 'text', 42, {}, {'id': 'x'}, {'id': 'x', 'rawId': 'x', 'response': 'no'}])
async def test_registration_rejects_malformed_credentials(fake_redis, garbage) -> None:
    with pytest.raises(passkey.PasskeyVerificationError):
        passkey.verify_registration(challenge=b'c' * 32, credential=garbage)


async def test_accepted_origins_are_exactly_the_configured_ones(fake_redis, monkeypatch) -> None:
    monkeypatch.setattr(passkey.settings, 'PASSKEY_ORIGINS', 'https://a.watchd0g.dev, https://b.watchd0g.dev/')
    for origin in ('https://a.watchd0g.dev', 'https://b.watchd0g.dev'):
        authenticator = _authenticator()
        authenticator.origin = origin
        verified = await _verified_registration(authenticator)
        assert verified.credential_id == authenticator.credential_id

    outsider = _authenticator()
    outsider.origin = 'https://my.watchd0g.dev'  # поддомен rp_id, но не в списке
    challenge_id, _, credential = await _register(outsider)
    challenge = await passkey.consume_challenge(challenge_id, purpose=passkey.PURPOSE_REGISTER, user_id=USER_ID)
    with pytest.raises(passkey.PasskeyVerificationError):
        passkey.verify_registration(challenge=challenge, credential=credential)


# --- вход ---------------------------------------------------------------------


async def test_login_options_use_discoverable_credentials_and_require_user_verification(fake_redis) -> None:
    _, options = await passkey.create_login_options()

    assert options['rpId'] == PASSKEY_RP_ID
    assert options['userVerification'] == 'required'
    assert options.get('allowCredentials', []) == []


async def _login_setup(authenticator: SoftwareAuthenticator, *, stored_count: int = 0):
    verified = await _verified_registration(authenticator)
    challenge_id, options = await passkey.create_login_options()
    challenge = await passkey.consume_challenge(challenge_id, purpose=passkey.PURPOSE_LOGIN)
    return verified, options, challenge, stored_count


async def test_login_verifies_a_real_assertion(fake_redis) -> None:
    authenticator = _authenticator()
    verified, options, challenge, _ = await _login_setup(authenticator)

    result = passkey.verify_login(
        challenge=challenge,
        credential=authenticator.get(options),
        public_key=verified.public_key,
        sign_count=0,
    )

    assert result.sign_count == 1
    assert result.credential_id == authenticator.credential_id


async def test_credential_id_is_read_from_the_assertion(fake_redis) -> None:
    authenticator = _authenticator()
    _, options = await passkey.create_login_options()

    assert passkey.credential_id_of(authenticator.get(options)) == authenticator.credential_id


@pytest.mark.parametrize('garbage', [None, 'text', {}, {'rawId': 5}, {'rawId': '***'}])
async def test_credential_id_of_rejects_malformed_input(garbage) -> None:
    with pytest.raises(passkey.PasskeyVerificationError):
        passkey.credential_id_of(garbage)


async def test_login_rejects_a_signature_from_another_key(fake_redis) -> None:
    authenticator = _authenticator()
    verified, options, challenge, _ = await _login_setup(authenticator)
    forged = authenticator.get(options, signing_key=authenticator.foreign_key())

    with pytest.raises(passkey.PasskeyVerificationError):
        passkey.verify_login(challenge=challenge, credential=forged, public_key=verified.public_key, sign_count=0)


async def test_login_rejects_a_foreign_origin(fake_redis) -> None:
    authenticator = _authenticator()
    verified, options, challenge, _ = await _login_setup(authenticator)

    with pytest.raises(passkey.PasskeyVerificationError):
        passkey.verify_login(
            challenge=challenge,
            credential=authenticator.get(options, origin='https://evil.example'),
            public_key=verified.public_key,
            sign_count=0,
        )


async def test_login_rejects_a_foreign_rp_id(fake_redis) -> None:
    authenticator = _authenticator()
    verified, options, challenge, _ = await _login_setup(authenticator)

    with pytest.raises(passkey.PasskeyVerificationError):
        passkey.verify_login(
            challenge=challenge,
            credential=authenticator.get(options, rp_id='evil.example'),
            public_key=verified.public_key,
            sign_count=0,
        )


async def test_login_rejects_when_user_was_not_verified(fake_redis) -> None:
    authenticator = _authenticator()
    verified, options, challenge, _ = await _login_setup(authenticator)

    with pytest.raises(passkey.PasskeyVerificationError):
        passkey.verify_login(
            challenge=challenge,
            credential=authenticator.get(options, user_verified=False),
            public_key=verified.public_key,
            sign_count=0,
        )


async def test_login_rejects_a_challenge_the_server_did_not_issue(fake_redis) -> None:
    authenticator = _authenticator()
    verified, options, challenge, _ = await _login_setup(authenticator)

    with pytest.raises(passkey.PasskeyVerificationError):
        passkey.verify_login(
            challenge=challenge,
            credential=authenticator.get(options, challenge=b64u(b'y' * 32)),
            public_key=verified.public_key,
            sign_count=0,
        )


async def test_login_rejects_a_registration_type_in_client_data(fake_redis) -> None:
    authenticator = _authenticator()
    verified, options, challenge, _ = await _login_setup(authenticator)

    with pytest.raises(passkey.PasskeyVerificationError):
        passkey.verify_login(
            challenge=challenge,
            credential=authenticator.get(options, client_data_type='webauthn.create'),
            public_key=verified.public_key,
            sign_count=0,
        )


@pytest.mark.parametrize(
    ('stored', 'presented', 'accepted'),
    [
        (0, 0, True),  # устройство без счётчика (iCloud Keychain, Google Password Manager)
        (0, 1, True),
        (5, 6, True),
        (5, 5, False),  # повтор счётчика: клон или replay
        (5, 3, False),  # откат счётчика: клон
        (5, 0, False),  # счётчик «обнулился» у ключа, который считал
    ],
)
async def test_login_sign_count_regression_is_refused(fake_redis, stored, presented, accepted) -> None:
    authenticator = _authenticator()
    verified, options, challenge, _ = await _login_setup(authenticator)
    credential = authenticator.get(options, sign_count=presented)

    def verify():
        return passkey.verify_login(
            challenge=challenge, credential=credential, public_key=verified.public_key, sign_count=stored
        )

    if accepted:
        assert verify().sign_count == presented
    else:
        with pytest.raises(passkey.PasskeyVerificationError):
            verify()


async def test_login_exposes_user_handle_for_the_caller_to_compare(fake_redis) -> None:
    authenticator = _authenticator()
    verified, options, challenge, _ = await _login_setup(authenticator)

    result = passkey.verify_login(
        challenge=challenge, credential=authenticator.get(options), public_key=verified.public_key, sign_count=0
    )

    assert result.user_handle == passkey.user_handle_for(USER_ID)
