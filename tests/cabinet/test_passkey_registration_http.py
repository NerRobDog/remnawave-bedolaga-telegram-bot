"""Регистрация passkey через HTTP: JWT, challenge, лимиты, подмена чужого credential."""

from __future__ import annotations

import pytest

from app.cabinet.auth import passkey
from app.config import settings
from app.database.models import UserPasskey
from tests.cabinet.passkey_harness import new_authenticator, open_harness
from tests.fixtures.webauthn_authenticator import b64u


pytestmark = [pytest.mark.usefixtures('passkey_settings'), pytest.mark.asyncio]

REGISTER_OPTIONS = '/cabinet/passkey/register/options'
REGISTER_VERIFY = '/cabinet/passkey/register/verify'
KEY_MATERIAL_MARKERS = ('public_key', 'publicKey', 'credential_id', 'sign_count', 'attestation')


# --- доступность -------------------------------------------------------------


async def test_status_reports_whether_passkey_is_configured(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        on = await h.client.get('/cabinet/auth/passkey/status')
        monkeypatch.setattr(settings, 'PASSKEY_ENABLED', False)
        off = await h.client.get('/cabinet/auth/passkey/status')

    assert on.status_code == 200 and on.json() == {'enabled': True}
    assert off.status_code == 200 and off.json() == {'enabled': False}


@pytest.mark.parametrize(
    'overrides',
    [{'PASSKEY_ENABLED': False}, {'PASSKEY_RP_ID': ''}, {'PASSKEY_ORIGINS': ''}],
    ids=['switched-off', 'no-rp-id', 'no-origin'],
)
async def test_every_endpoint_answers_passkey_disabled_when_not_configured(monkeypatch, fake_redis, overrides) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        user = await h.make_user()
        for name, value in overrides.items():
            monkeypatch.setattr(settings, name, value)

        responses = [
            await h.login_options(),
            await h.login_verify('c' * 32, {}),
            await h.register_options(user),
            await h.register_verify(user, 'c' * 32, {}),
            await h.client.get('/cabinet/passkey', headers=h.auth(user)),
            await h.client.patch('/cabinet/passkey/1', json={'name': 'x'}, headers=h.auth(user)),
            await h.client.delete('/cabinet/passkey/1', headers=h.auth(user)),
        ]

    for response in responses:
        assert response.status_code == 404, response.text
        assert response.json()['detail']['code'] == 'passkey_disabled'


# --- регистрация -------------------------------------------------------------


async def test_registration_round_trip_stores_the_key_and_leaks_no_key_material(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        user = await h.make_user(email='alice@example.org', first_name='Alice')

        options_response = await h.register_options(user)
        options = options_response.json()
        authenticator = new_authenticator(backed_up=True)
        verify = await h.register_verify(user, options['challenge_id'], authenticator.create(options['options']))
        rows = await h.passkeys(user)

    assert options_response.status_code == 200
    assert options['options']['user']['name'] == 'alice@example.org'
    assert options['options']['user']['displayName'] == 'Alice'
    assert verify.status_code == 201, verify.text
    body = verify.json()
    assert body['name'] == 'Passkey 1'
    assert body['device_type'] == 'multi_device' and body['backed_up'] is True
    assert body['transports'] == ['internal']
    assert body['last_used_at'] is None
    assert not any(marker in verify.text for marker in KEY_MATERIAL_MARKERS)

    assert len(rows) == 1
    assert rows[0].credential_id == authenticator.credential_id
    assert rows[0].user_id == user.id and rows[0].sign_count == 0
    assert rows[0].public_key and rows[0].public_key != rows[0].credential_id


async def test_custom_name_is_trimmed_and_stored(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        user = await h.make_user()
        _, verify = await h.register(user, name='  MacBook Touch ID  ')

    assert verify.status_code == 201
    assert verify.json()['name'] == 'MacBook Touch ID'


@pytest.mark.parametrize('name', ['', '   ', 'x' * 101])
async def test_unusable_names_are_refused(monkeypatch, fake_redis, name) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        user = await h.make_user()
        _, verify = await h.register(user, name=name)
        rows = await h.passkeys(user)

    assert verify.status_code == 422
    assert rows == []


async def test_registration_endpoints_require_a_session(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        options = await h.post(REGISTER_OPTIONS)
        verify = await h.post(REGISTER_VERIFY, challenge_id='c' * 32, credential={})
        forged = await h.client.post(REGISTER_OPTIONS, headers={'Authorization': 'Bearer not-a-jwt'})

    assert options.status_code == 401
    assert verify.status_code == 401
    assert forged.status_code == 401


async def test_already_registered_keys_are_excluded_from_new_options(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        user = await h.make_user()
        first, _ = await h.register(user)
        options = (await h.register_options(user)).json()['options']

    assert [item['id'] for item in options['excludeCredentials']] == [b64u(first.credential_id)]
    assert options['excludeCredentials'][0]['transports'] == ['internal']


async def test_repeating_a_registration_of_the_same_credential_is_refused(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        user = await h.make_user()
        authenticator, first = await h.register(user)
        _, again = await h.register(user, authenticator)
        rows = await h.passkeys(user)

    assert first.status_code == 201
    assert again.status_code == 409
    assert again.json()['detail']['code'] == 'passkey_already_registered'
    assert len(rows) == 1


async def test_credential_id_of_another_account_cannot_be_claimed(monkeypatch, fake_redis) -> None:
    """Ключ жертвы нельзя «занять» своим публичным ключом под тем же credential id."""
    async with open_harness(monkeypatch, fake_redis) as h:
        victim = await h.make_user()
        attacker = await h.make_user()
        victim_key, _ = await h.register(victim)
        stored_before = (await h.passkeys(victim))[0].public_key

        squatter = new_authenticator(credential_id=victim_key.credential_id)
        _, response = await h.register(attacker, squatter)

        victim_rows = await h.passkeys(victim)
        attacker_rows = await h.passkeys(attacker)

    assert response.status_code == 409
    assert attacker_rows == []
    assert [row.public_key for row in victim_rows] == [stored_before]


# --- челлендж ----------------------------------------------------------------


async def test_challenge_issued_to_another_user_is_refused_and_burned(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        owner = await h.make_user()
        thief = await h.make_user()
        options = (await h.register_options(owner)).json()
        credential = new_authenticator().create(options['options'])

        stolen = await h.register_verify(thief, options['challenge_id'], credential)
        late = await h.register_verify(owner, options['challenge_id'], credential)
        rows = await h.passkeys()

    assert stolen.status_code == 400 and stolen.json()['detail']['code'] == 'invalid_challenge'
    assert late.status_code == 400 and late.json()['detail']['code'] == 'invalid_challenge'
    assert rows == []


async def test_a_challenge_works_once(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        user = await h.make_user()
        options = (await h.register_options(user)).json()
        credential = new_authenticator().create(options['options'])

        first = await h.register_verify(user, options['challenge_id'], credential)
        replay = await h.register_verify(user, options['challenge_id'], credential)
        rows = await h.passkeys(user)

    assert first.status_code == 201
    assert replay.status_code == 400 and replay.json()['detail']['code'] == 'invalid_challenge'
    assert len(rows) == 1


async def test_an_expired_challenge_is_refused(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        user = await h.make_user()
        options = (await h.register_options(user)).json()
        credential = new_authenticator().create(options['options'])

        fake_redis.advance(passkey.CHALLENGE_TTL_SECONDS + 1)
        response = await h.register_verify(user, options['challenge_id'], credential)

    assert response.status_code == 400 and response.json()['detail']['code'] == 'invalid_challenge'


async def test_a_login_challenge_cannot_register_a_key(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        user = await h.make_user()
        login = (await h.login_options()).json()
        registration = (await h.register_options(user)).json()
        credential = new_authenticator().create(registration['options'])
        response = await h.register_verify(user, login['challenge_id'], credential)

    assert response.status_code == 400 and response.json()['detail']['code'] == 'invalid_challenge'


# --- проверка ответа аутентификатора ------------------------------------------


@pytest.mark.parametrize(
    'create_kwargs',
    [
        {'origin': 'https://evil.example'},
        {'rp_id': 'evil.example'},
        {'user_verified': False},
        {'user_present': False},
        {'cross_origin': True},
        {'client_data_type': 'webauthn.get'},
    ],
    ids=['foreign-origin', 'foreign-rp-id', 'no-user-verification', 'no-user-presence', 'cross-origin', 'wrong-type'],
)
async def test_unverifiable_attestations_store_nothing(monkeypatch, fake_redis, create_kwargs) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        user = await h.make_user()
        options = (await h.register_options(user)).json()
        credential = new_authenticator().create(options['options'], **create_kwargs)

        response = await h.register_verify(user, options['challenge_id'], credential)
        rows = await h.passkeys()

    assert response.status_code == 400
    assert response.json()['detail']['code'] == 'verification_failed'
    assert rows == []


async def test_attestation_for_a_different_challenge_is_refused(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        user = await h.make_user()
        options = (await h.register_options(user)).json()
        credential = new_authenticator().create(options['options'], challenge=b64u(b'z' * 32))

        response = await h.register_verify(user, options['challenge_id'], credential)

    assert response.status_code == 400 and response.json()['detail']['code'] == 'verification_failed'


@pytest.mark.parametrize('credential', [None, 'text', 7, [], {}, {'id': 'x', 'rawId': 'x', 'response': {}}])
async def test_malformed_credentials_are_a_client_error_not_a_crash(monkeypatch, fake_redis, credential) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        user = await h.make_user()
        options = (await h.register_options(user)).json()
        response = await h.post(REGISTER_VERIFY, user, challenge_id=options['challenge_id'], credential=credential)

    assert response.status_code in (400, 422), response.text


async def test_oversized_credentials_are_refused_before_parsing(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        user = await h.make_user()
        options = (await h.register_options(user)).json()
        response = await h.post(
            REGISTER_VERIFY, user, challenge_id=options['challenge_id'], credential={'blob': 'A' * 70_000}
        )

    assert response.status_code == 422


# --- лимиты ------------------------------------------------------------------


async def test_ten_keys_per_user_is_the_ceiling(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        user = await h.make_user()
        for index in range(10):
            h.db.add(
                UserPasskey(
                    user_id=user.id,
                    credential_id=bytes([index]) * 32,
                    public_key=b'k',
                    sign_count=0,
                    device_type='single_device',
                    backed_up=False,
                    name=f'Key {index}',
                )
            )
        await h.db.commit()

        options = await h.register_options(user)
        rows = await h.passkeys(user)

    assert options.status_code == 400
    assert options.json()['detail']['code'] == 'passkey_limit_reached'
    assert len(rows) == 10


async def test_ceiling_is_enforced_again_at_verify_time(monkeypatch, fake_redis) -> None:
    """Options выданы при девяти ключах, десятый успел появиться — одиннадцатый не проходит."""
    async with open_harness(monkeypatch, fake_redis) as h:
        user = await h.make_user()
        for index in range(9):
            h.db.add(
                UserPasskey(
                    user_id=user.id,
                    credential_id=bytes([index]) * 32,
                    public_key=b'k',
                    device_type='single_device',
                    name=f'Key {index}',
                )
            )
        await h.db.commit()
        options = (await h.register_options(user)).json()
        credential = new_authenticator().create(options['options'])

        h.db.add(
            UserPasskey(user_id=user.id, credential_id=b'\xaa' * 32, public_key=b'k', device_type='x', name='Late')
        )
        await h.db.commit()
        response = await h.register_verify(user, options['challenge_id'], credential)
        rows = await h.passkeys(user)

    assert response.status_code == 400 and response.json()['detail']['code'] == 'passkey_limit_reached'
    assert len(rows) == 10


async def test_registration_options_are_rate_limited_per_user(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        user = await h.make_user()
        other = await h.make_user()
        statuses = [(await h.register_options(user)).status_code for _ in range(11)]
        other_status = (await h.register_options(other)).status_code

    assert statuses[:10] == [200] * 10
    assert statuses[10] == 429
    assert other_status == 200


async def test_registration_fails_closed_when_the_rate_limiter_is_down(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        user = await h.make_user()
        monkeypatch.setattr(passkey.cache, '_connected', False)
        response = await h.register_options(user)

    assert response.status_code == 429


async def test_passkey_routes_are_mounted_on_the_cabinet_router() -> None:
    from fastapi import FastAPI

    from app.cabinet.routes import router

    app = FastAPI()
    app.include_router(router)
    # app.router.routes в FastAPI 0.141 держит включённые роутеры лениво; схема OpenAPI раскрывает их все.
    mounted = {(method.upper(), path) for path, operations in app.openapi()['paths'].items() for method in operations}

    assert {
        ('GET', '/cabinet/auth/passkey/status'),
        ('POST', '/cabinet/auth/passkey/login/options'),
        ('POST', '/cabinet/auth/passkey/login/verify'),
        ('POST', '/cabinet/passkey/register/options'),
        ('POST', '/cabinet/passkey/register/verify'),
        ('GET', '/cabinet/passkey'),
        ('PATCH', '/cabinet/passkey/{passkey_id}'),
        ('DELETE', '/cabinet/passkey/{passkey_id}'),
    } <= mounted
