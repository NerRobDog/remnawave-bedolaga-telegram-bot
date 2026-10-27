"""Вход по recovery-коду и управление кодами через HTTP: одноразовость, неразличимые отказы, лимиты."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re

import pytest
from structlog.testing import capture_logs

from app.cabinet.auth import get_token_payload, passkey, recovery_codes
from app.config import settings
from app.database.models import User, UserStatus
from tests.cabinet.passkey_harness import open_harness


pytestmark = [pytest.mark.usefixtures('passkey_settings', 'passkey_signup_settings'), pytest.mark.asyncio]

SHOWN_CODE = re.compile(r'[0-9A-Z]{4}(-[0-9A-Z]{4}){3}')
REFUSAL = {'detail': 'Recovery code is invalid'}


async def _account(h):
    """Аккаунт, созданный passkey: (user_id, показанные коды)."""
    _, response = await h.signup()
    assert response.status_code == 200, response.text
    body = response.json()
    return body['user']['id'], body['recovery_codes']


async def _set_status(h, user_id: int, value: str) -> None:
    user = await h.db.get(User, user_id, populate_existing=True)
    user.status = value
    await h.db.commit()
    h.db.expunge(user)


# --- удачный вход ------------------------------------------------------------


async def test_a_recovery_code_logs_in_once_and_marks_the_login_as_recovery(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        user_id, codes = await _account(h)

        response = await h.recovery_login(codes[0])
        replay = await h.recovery_login(codes[0])
        tokens = await h.refresh_tokens()
        rows = await h.recovery_rows(user_id)
        user = await h.db.get(User, user_id, populate_existing=True)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body['recovery_login'] is True
    assert body['user']['id'] == user_id and body['user']['auth_type'] == 'passkey'
    assert get_token_payload(body['access_token'], expected_type='access')['sub'] == str(user_id)
    assert get_token_payload(body['refresh_token'], expected_type='refresh')['sub'] == str(user_id)
    assert hashlib.sha256(body['refresh_token'].encode()).hexdigest() in {t.token_hash for t in tokens}
    assert replay.status_code == 401 and replay.json() == REFUSAL
    assert sum(row.used_at is not None for row in rows) == 1
    assert user.cabinet_last_login is not None
    assert response.headers['Cache-Control'] == 'no-store'


@pytest.mark.parametrize(
    'spell',
    [str.lower, lambda c: c.replace('-', ''), lambda c: f'  {c.lower()}  ', lambda c: c.replace('-', ' ')],
    ids=['lowercase', 'no-dashes', 'padded', 'spaces'],
)
async def test_the_code_is_accepted_however_a_human_retypes_it(monkeypatch, fake_redis, spell) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        _, codes = await _account(h)
        response = await h.recovery_login(spell(codes[0]))

    assert response.status_code == 200, response.text


async def test_every_code_of_the_set_works_exactly_once(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        user_id, codes = await _account(h)
        results = []
        for code in codes:
            fake_redis.advance(3600)  # окно входа по коду — 5 в минуту и 20 в час
            results.append((await h.recovery_login(code)).status_code)
        fake_redis.advance(3600)
        again = await h.recovery_login(codes[0])
        remaining = await h.recovery_status(User(id=user_id, telegram_id=None))

    assert results == [200] * 8
    assert again.status_code == 401
    assert remaining.json() == {'remaining': 0, 'total': 8}


# --- отказы: один и тот же ответ ---------------------------------------------------------


async def test_every_refusal_is_the_same_401_down_to_the_headers(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        user_id, codes = await _account(h)
        spent = codes[1]
        assert (await h.recovery_login(spent)).status_code == 200
        blocked_id, blocked_codes = await _account(h)
        await _set_status(h, blocked_id, UserStatus.BLOCKED.value)
        deleted_id, deleted_codes = await _account(h)
        await _set_status(h, deleted_id, UserStatus.DELETED.value)

        wrong_but_well_formed = recovery_codes.format_code(recovery_codes.generate_code())
        attempts = {
            'unknown': wrong_but_well_formed,
            'spent': spent,
            'malformed': 'not-a-code',
            'empty': '',
            'huge': 'A' * 5000,
            'blocked': blocked_codes[0],
            'deleted': deleted_codes[0],
        }
        responses = {}
        for name, code in attempts.items():
            fake_redis.advance(3600)
            responses[name] = await h.recovery_login(code)

    for name, response in responses.items():
        assert response.status_code == 401, name
        assert response.json() == REFUSAL, name

    def shape(response):
        return response.status_code, response.text, {k: v for k, v in response.headers.items() if k != 'date'}

    assert len({repr(shape(r)) for r in responses.values()}) == 1, 'отказы различимы'


async def test_the_code_of_an_inactive_account_is_spent_before_the_status_is_checked(monkeypatch, fake_redis) -> None:
    """Иначе по тому, сгорел ли код, можно было бы узнать статус чужого аккаунта."""
    async with open_harness(monkeypatch, fake_redis, session_per_request=True) as h:
        blocked_id, codes = await _account(h)
        await _set_status(h, blocked_id, UserStatus.BLOCKED.value)
        refused = await h.recovery_login(codes[0])
        rows = await h.recovery_rows(blocked_id)
        await _set_status(h, blocked_id, UserStatus.ACTIVE.value)
        fake_redis.advance(3600)
        after_unblock = await h.recovery_login(codes[0])

    assert refused.status_code == 401
    assert sum(row.used_at is not None for row in rows) == 1
    assert after_unblock.status_code == 401


async def test_a_refused_login_creates_no_session(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        await _account(h)
        before = len(await h.refresh_tokens())
        await h.recovery_login('0000-0000-0000-0000')
        after = len(await h.refresh_tokens())

    assert after == before


async def test_codes_are_never_logged_neither_when_accepted_nor_refused(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        _, codes = await _account(h)
        with capture_logs() as logs:
            await h.recovery_login(codes[0])
            await h.recovery_login(codes[0])
            await h.recovery_login('ABCD-EFGH-JKMN-PQRS')
            await h.recovery_regenerate(User(id=1, telegram_id=None))

    haystack = repr(logs).upper()
    typed = [codes[0], 'ABCD-EFGH-JKMN-PQRS'] + codes
    assert not any(code in haystack or code.replace('-', '') in haystack for code in typed)
    assert not any(recovery_codes.hash_code(code.replace('-', '')).upper() in haystack for code in typed)


async def test_login_by_code_is_off_when_passkey_is_not_configured(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        monkeypatch.setattr(settings, 'PASSKEY_ENABLED', False)
        response = await h.recovery_login('ABCD-EFGH-JKMN-PQRS')

    assert response.status_code == 404
    assert response.json()['detail']['code'] == 'passkey_disabled'


async def test_login_by_code_stays_open_after_signup_is_closed(monkeypatch, fake_redis) -> None:
    """Закрыть набор новых аккаунтов можно, лишив прежних владельцев аварийного входа — нельзя."""
    async with open_harness(monkeypatch, fake_redis) as h:
        _, codes = await _account(h)
        monkeypatch.setattr(settings, 'PASSKEY_SIGNUP_ENABLED', False)
        response = await h.recovery_login(codes[0])

    assert response.status_code == 200


# --- лимиты -----------------------------------------------------------------------------


async def test_five_attempts_a_minute_then_429_even_for_a_right_code(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        _, codes = await _account(h)
        wrong = [await h.recovery_login('ABCD-EFGH-JKMN-PQRS') for _ in range(5)]
        blocked = await h.recovery_login(codes[0])
        fake_redis.advance(61)
        later = await h.recovery_login(codes[0])

    assert [r.status_code for r in wrong] == [401] * 5
    assert blocked.status_code == 429 and blocked.headers['Retry-After'] == '60'
    assert later.status_code == 200


async def test_twenty_attempts_an_hour(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        statuses = []
        for _ in range(21):
            if len(statuses) % 5 == 0:
                fake_redis.advance(61)  # минутное окно сбрасывается, часовое копится
            statuses.append(await h.recovery_login('ABCD-EFGH-JKMN-PQRS'))

    assert [r.status_code for r in statuses[:20]] == [401] * 20
    assert statuses[20].status_code == 429 and statuses[20].headers['Retry-After'] == '3600'


async def test_the_limiter_fails_closed_without_redis(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        _, codes = await _account(h)
        monkeypatch.setattr(passkey.cache, '_connected', False)
        response = await h.recovery_login(codes[0])
        monkeypatch.setattr(passkey.cache, '_connected', True)
        rows = await h.recovery_rows()

    assert response.status_code == 429
    assert all(row.used_at is None for row in rows), 'при отказе лимитера код не тратится'


# --- гонка -----------------------------------------------------------------------------------


async def test_two_simultaneous_logins_with_one_code_yield_exactly_one_session(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis, session_per_request=True) as h:
        _, codes = await _account(h)
        results = await asyncio.gather(*(h.recovery_login(codes[0]) for _ in range(5)))
        rows = await h.recovery_rows()

    assert sorted(r.status_code for r in results) == [200, 401, 401, 401, 401]
    assert sum(row.used_at is not None for row in rows) == 1
    assert len([r for r in results if r.status_code == 200 and r.json()['recovery_login']]) == 1


# --- состояние кодов -------------------------------------------------------------------------


async def test_status_counts_the_unused_codes_and_needs_a_session(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        user_id, codes = await _account(h)
        owner = User(id=user_id, telegram_id=None)
        full = await h.recovery_status(owner)
        await h.recovery_login(codes[0])
        used_one = await h.recovery_status(owner)
        anonymous = await h.client.get('/cabinet/passkey/recovery-codes')
        nobody = await h.make_user()
        empty = await h.recovery_status(nobody)

    assert full.json() == {'remaining': 8, 'total': 8}
    assert used_one.json() == {'remaining': 7, 'total': 8}
    assert anonymous.status_code in (401, 403)
    assert empty.json() == {'remaining': 0, 'total': 0}


async def test_status_never_shows_the_codes_or_their_hashes(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        user_id, _ = await _account(h)
        body = (await h.recovery_status(User(id=user_id, telegram_id=None))).text

    assert set(json.loads(body)) == {'remaining', 'total'}


async def test_regenerate_replaces_the_whole_set_and_shows_the_new_codes_once(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        user_id, old_codes = await _account(h)
        owner = User(id=user_id, telegram_id=None)
        await h.recovery_login(old_codes[0])  # одним кодом уже воспользовались

        regenerated = await h.recovery_regenerate(owner)
        fake_redis.advance(3600)
        old_unused = await h.recovery_login(old_codes[1])
        fake_redis.advance(3600)
        status_after = await h.recovery_status(owner)
        new_codes = regenerated.json()['recovery_codes']
        new_works = await h.recovery_login(new_codes[0])
        rows = await h.recovery_rows(user_id)

    assert regenerated.status_code == 200, regenerated.text
    body = regenerated.json()
    assert len(new_codes) == 8 and all(SHOWN_CODE.fullmatch(code) for code in new_codes)
    assert not set(new_codes) & set(old_codes)
    assert (body['remaining'], body['total']) == (8, 8)
    assert regenerated.headers['Cache-Control'] == 'no-store'
    assert old_unused.status_code == 401
    assert status_after.json() == {'remaining': 8, 'total': 8}
    assert new_works.status_code == 200
    assert len(rows) == 8, 'потраченные и неиспользованные старые коды уничтожены'


async def test_regenerate_leaves_other_accounts_alone(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        first_id, first_codes = await _account(h)
        second_id, _ = await _account(h)
        await h.recovery_regenerate(User(id=second_id, telegram_id=None))
        fake_redis.advance(3600)
        response = await h.recovery_login(first_codes[0])

    assert response.status_code == 200 and response.json()['user']['id'] == first_id


async def test_regenerate_works_for_an_account_that_has_no_codes_yet(monkeypatch, fake_redis) -> None:
    """Запасной вход полезен и тем, кто пришёл через Telegram или почту."""
    async with open_harness(monkeypatch, fake_redis) as h:
        user = await h.make_user(telegram_id=777)
        response = await h.recovery_regenerate(user)
        fake_redis.advance(3600)
        login = await h.recovery_login(response.json()['recovery_codes'][0])

    assert response.status_code == 200
    assert login.status_code == 200 and login.json()['user']['id'] == user.id


async def test_regenerate_is_limited_to_three_an_hour_per_user(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        owner = await h.make_user()
        other = await h.make_user()
        statuses = [(await h.recovery_regenerate(owner)).status_code for _ in range(4)]
        other_ok = await h.recovery_regenerate(other)
        fake_redis.advance(3601)
        later = await h.recovery_regenerate(owner)
        rows = await h.recovery_rows(owner.id)

    assert statuses == [200, 200, 200, 429]
    assert other_ok.status_code == 200 and later.status_code == 200
    assert len(rows) == 8


async def test_regenerate_needs_a_session_and_an_enabled_feature(monkeypatch, fake_redis) -> None:
    async with open_harness(monkeypatch, fake_redis) as h:
        anonymous = await h.client.post('/cabinet/passkey/recovery-codes/regenerate')
        user = await h.make_user()
        monkeypatch.setattr(settings, 'PASSKEY_ENABLED', False)
        disabled = await h.recovery_regenerate(user)
        status_disabled = await h.recovery_status(user)

    assert anonymous.status_code in (401, 403)
    assert disabled.status_code == status_disabled.status_code == 404
