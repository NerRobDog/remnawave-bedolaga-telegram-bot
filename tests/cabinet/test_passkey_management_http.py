"""Список, переименование и удаление passkey: чужие ключи недоступны, последний способ входа не удалить."""

from __future__ import annotations

import pytest

from app.cabinet.auth import hash_password
from tests.cabinet.passkey_harness import open_harness


pytestmark = [pytest.mark.usefixtures('passkey_settings'), pytest.mark.asyncio]

KEY_MATERIAL_MARKERS = ('public_key', 'publicKey', 'credential_id', 'credentialId', 'sign_count')


async def _register_n(h, user, count: int):
    authenticators = []
    for _ in range(count):
        authenticator, response = await h.register(user)
        assert response.status_code == 201, response.text
        authenticators.append((authenticator, response.json()['id']))
    return authenticators


# --- список ------------------------------------------------------------------


async def test_list_shows_only_own_keys_without_key_material(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        alice = await h.make_user()
        bob = await h.make_user()
        await h.register(alice, name='Alice laptop')
        await h.register(bob, name='Bob phone')

        response = await h.client.get('/cabinet/passkey', headers=h.auth(alice))

    body = response.json()
    assert response.status_code == 200
    assert [item['name'] for item in body['passkeys']] == ['Alice laptop']
    assert body['limit'] == 10
    assert set(body['passkeys'][0]) == {
        'id',
        'name',
        'device_type',
        'backed_up',
        'transports',
        'created_at',
        'last_used_at',
    }
    assert not any(marker in response.text for marker in KEY_MATERIAL_MARKERS)


async def test_management_endpoints_need_a_session(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        listing = await h.client.get('/cabinet/passkey')
        rename = await h.client.patch('/cabinet/passkey/1', json={'name': 'x'})
        delete = await h.client.delete('/cabinet/passkey/1')

    assert (listing.status_code, rename.status_code, delete.status_code) == (401, 401, 401)


# --- переименование ----------------------------------------------------------


async def test_rename_own_key(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        user = await h.make_user()
        _, created = await h.register(user)
        passkey_id = created.json()['id']

        response = await h.client.patch(
            f'/cabinet/passkey/{passkey_id}', json={'name': '  YubiKey 5C  '}, headers=h.auth(user)
        )
        rows = await h.passkeys(user)

    assert response.status_code == 200 and response.json()['name'] == 'YubiKey 5C'
    assert rows[0].name == 'YubiKey 5C'


async def test_rename_someone_elses_key_looks_like_a_missing_key(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        owner = await h.make_user()
        intruder = await h.make_user()
        _, created = await h.register(owner, name='Original')
        passkey_id = created.json()['id']

        response = await h.client.patch(
            f'/cabinet/passkey/{passkey_id}', json={'name': 'Hijacked'}, headers=h.auth(intruder)
        )
        missing = await h.client.patch('/cabinet/passkey/99999', json={'name': 'x'}, headers=h.auth(intruder))
        rows = await h.passkeys(owner)

    assert response.status_code == 404
    assert response.json() == missing.json()
    assert rows[0].name == 'Original'


@pytest.mark.parametrize('name', ['', '   ', 'x' * 101])
async def test_rename_rejects_unusable_names(monkeypatch, fake_redis, name) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        user = await h.make_user()
        _, created = await h.register(user, name='Keep me')

        response = await h.client.patch(
            f'/cabinet/passkey/{created.json()["id"]}', json={'name': name}, headers=h.auth(user)
        )
        rows = await h.passkeys(user)

    assert response.status_code == 422
    assert rows[0].name == 'Keep me'


# --- удаление ----------------------------------------------------------------


async def test_delete_own_key_when_another_way_to_sign_in_remains(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        user = await h.make_user(telegram_id=777)
        _, created = await h.register(user)

        response = await h.client.delete(f'/cabinet/passkey/{created.json()["id"]}', headers=h.auth(user))
        rows = await h.passkeys(user)

    assert response.status_code == 200 and response.json() == {'success': True}
    assert rows == []


async def test_deleting_someone_elses_key_looks_like_a_missing_key_and_changes_nothing(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        owner = await h.make_user()
        intruder = await h.make_user(telegram_id=888)
        _, created = await h.register(owner)

        response = await h.client.delete(f'/cabinet/passkey/{created.json()["id"]}', headers=h.auth(intruder))
        missing = await h.client.delete('/cabinet/passkey/99999', headers=h.auth(intruder))
        rows = await h.passkeys(owner)

    assert response.status_code == 404
    assert response.json() == missing.json()
    assert len(rows) == 1


async def test_the_only_way_to_sign_in_cannot_be_deleted(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        user = await h.make_user()  # без пароля, Telegram и соцсетей
        _, created = await h.register(user)

        response = await h.client.delete(f'/cabinet/passkey/{created.json()["id"]}', headers=h.auth(user))
        rows = await h.passkeys(user)

    assert response.status_code == 400
    assert response.json()['detail']['code'] == 'last_login_method'
    assert len(rows) == 1


async def test_a_password_without_an_email_is_not_a_way_to_sign_in(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        user = await h.make_user(password_hash=hash_password('Str0ng-Passw0rd!'))
        _, created = await h.register(user)

        response = await h.client.delete(f'/cabinet/passkey/{created.json()["id"]}', headers=h.auth(user))

    assert response.status_code == 400
    assert response.json()['detail']['code'] == 'last_login_method'


@pytest.mark.parametrize(
    'remaining_method',
    [
        {'telegram_id': 4242},
        {'email': 'bob@example.org', 'password_hash': 'hash'},
        {'google_id': 'google-sub'},
        {'yandex_id': 'yandex-id'},
        {'discord_id': 'discord-id'},
        {'vk_id': 99},
    ],
    ids=['telegram', 'email+password', 'google', 'yandex', 'discord', 'vk'],
)
async def test_any_other_login_method_allows_deleting_the_last_key(monkeypatch, fake_redis, remaining_method) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        user = await h.make_user(**remaining_method)
        _, created = await h.register(user)

        response = await h.client.delete(f'/cabinet/passkey/{created.json()["id"]}', headers=h.auth(user))
        rows = await h.passkeys(user)

    assert response.status_code == 200
    assert rows == []


async def test_another_key_counts_as_a_login_method_until_it_is_the_last(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        user = await h.make_user()
        (_, first_id), (_, second_id) = await _register_n(h, user, 2)

        first = await h.client.delete(f'/cabinet/passkey/{first_id}', headers=h.auth(user))
        second = await h.client.delete(f'/cabinet/passkey/{second_id}', headers=h.auth(user))
        rows = await h.passkeys(user)

    assert first.status_code == 200
    assert second.status_code == 400 and second.json()['detail']['code'] == 'last_login_method'
    assert [row.id for row in rows] == [second_id]


async def test_deleted_key_can_no_longer_log_in(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        user = await h.make_user(telegram_id=999)
        authenticator, created = await h.register(user)
        await h.client.delete(f'/cabinet/passkey/{created.json()["id"]}', headers=h.auth(user))

        response = await h.login(authenticator)

    assert response.status_code == 401
