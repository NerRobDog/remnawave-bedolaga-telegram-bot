"""Журнал личностей триала: ключи, нормализация почты, хэширование и атомарное занятие.

В журнал кладутся только хэши ключей ``tg:<id>``, ``email:<нормализованный>``, ``google:<sub>``
(плюс остальные OAuth-провайдеры). Личность, уже попавшая в журнал, второй триал не получает,
как бы ни назывался новый аккаунт.
"""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.config import settings
from app.database.models import TrialIdentityLedger
from app.services import trial_identity as identity
from app.services.trial_eligibility import LiteTrialReason, LiteTrialRefused
from tests.fixtures.sqlite_memory import memory_session


def _user(**fields) -> SimpleNamespace:
    values = dict(
        telegram_id=None,
        email=None,
        email_verified=False,
        google_id=None,
        yandex_id=None,
        discord_id=None,
        vk_id=None,
        trial_reset_at=None,
    )
    values.update(fields)
    return SimpleNamespace(**values)


@pytest.fixture(autouse=True)
def _pepper(monkeypatch):
    monkeypatch.setattr(settings, 'CABINET_JWT_SECRET', 'trial-identity-tests-' + 'k' * 40)


# -- нормализация почты -----------------------------------------------------


@pytest.mark.parametrize(
    ('raw', 'expected'),
    [
        ('User@Example.COM', 'user@example.com'),
        ('  user@example.com  ', 'user@example.com'),
        ('john.smith@gmail.com', 'johnsmith@gmail.com'),
        ('J.O.H.N+promo@Gmail.com', 'john@gmail.com'),
        ('john+a+b@gmail.com', 'john@gmail.com'),
        ('john.smith@googlemail.com', 'johnsmith@gmail.com'),
        # у остальных доменов точка и плюс значимы — склеивать их нельзя
        ('john.smith@example.com', 'john.smith@example.com'),
        ('john+tag@example.com', 'john+tag@example.com'),
        ('john.smith@mail.ru', 'john.smith@mail.ru'),
    ],
)
def test_email_normalization(raw, expected) -> None:
    assert identity.normalize_email(raw) == expected


@pytest.mark.parametrize('raw', [None, '', '   ', 'no-at-sign', '@gmail.com', 'user@', '+tag@gmail.com'])
def test_unusable_email_has_no_identity(raw) -> None:
    assert identity.normalize_email(raw) is None


# -- ключи личности ----------------------------------------------------------


def test_keys_cover_telegram_verified_email_and_google() -> None:
    user = _user(telegram_id=42, email='John.Smith@gmail.com', email_verified=True, google_id='1234567890')

    assert identity.identity_keys(user) == ['tg:42', 'email:johnsmith@gmail.com', 'google:1234567890']


def test_keys_skip_what_the_account_does_not_have() -> None:
    assert identity.identity_keys(_user()) == []
    assert identity.identity_keys(_user(google_id='g1')) == ['google:g1']


def test_an_unverified_email_is_not_an_identity() -> None:
    """Непроверенный адрес может принадлежать другому человеку: сжигать его ключ нельзя."""
    assert identity.identity_keys(_user(email='someone@example.com', email_verified=False)) == []


def test_other_oauth_providers_are_covered_too() -> None:
    user = _user(yandex_id='y1', discord_id='d1', vk_id=77)

    assert identity.identity_keys(user) == ['yandex:y1', 'discord:d1', 'vk:77']


# -- хэширование -------------------------------------------------------------


def test_hash_is_a_64_char_hex_digest() -> None:
    assert re.fullmatch(r'[0-9a-f]{64}', identity.hash_identity('tg:42'))


def test_hash_is_stable_and_distinguishes_keys() -> None:
    assert identity.hash_identity('tg:42') == identity.hash_identity('tg:42')
    assert identity.hash_identity('tg:42') != identity.hash_identity('tg:43')
    assert identity.hash_identity('tg:42') != identity.hash_identity('google:42')


def test_hash_depends_on_the_pepper(monkeypatch) -> None:
    before = identity.hash_identity('tg:42')
    monkeypatch.setattr(settings, 'CABINET_JWT_SECRET', 'another-secret-' + 'z' * 40)

    assert identity.hash_identity('tg:42') != before


def test_hash_does_not_contain_the_plain_value() -> None:
    digest = identity.hash_identity('email:john@example.com')

    assert 'john' not in digest and 'example' not in digest


# -- журнал в БД -------------------------------------------------------------


async def _rows(db) -> list[str]:
    return list((await db.execute(select(TrialIdentityLedger.identity_hash))).scalars())


@pytest.mark.asyncio
async def test_claim_stores_only_hashes(monkeypatch) -> None:
    async with memory_session(monkeypatch, [TrialIdentityLedger.__table__]) as db:
        await identity.claim_identities(db, _user(telegram_id=42, google_id='g1'))
        await db.commit()

        stored = await _rows(db)

    assert sorted(stored) == sorted([identity.hash_identity('tg:42'), identity.hash_identity('google:g1')])
    assert all(re.fullmatch(r'[0-9a-f]{64}', value) for value in stored)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('first', 'second'),
    [
        (dict(telegram_id=42), dict(telegram_id=42)),
        (dict(google_id='g1'), dict(google_id='g1', email='other@example.com', email_verified=True)),
        (
            dict(email='john.smith@gmail.com', email_verified=True),
            dict(email='johnsmith+again@googlemail.com', email_verified=True),
        ),
        (dict(telegram_id=42, google_id='g1'), dict(google_id='g1')),
        (dict(vk_id=77), dict(vk_id=77)),
    ],
    ids=['same telegram', 'same google sub', 'same gmail', 'one shared key of two', 'same vk'],
)
async def test_a_known_identity_gets_no_second_trial(monkeypatch, first, second) -> None:
    async with memory_session(monkeypatch, [TrialIdentityLedger.__table__]) as db:
        await identity.claim_identities(db, _user(**first))
        await db.commit()

        with pytest.raises(LiteTrialRefused) as refused:
            await identity.ensure_identities_free(db, _user(**second))
        assert refused.value.reason == LiteTrialReason.IDENTITY_ALREADY_USED

        with pytest.raises(LiteTrialRefused):
            await identity.claim_identities(db, _user(**second))


@pytest.mark.asyncio
async def test_a_refused_claim_stores_nothing_from_that_account(monkeypatch) -> None:
    """Если один ключ занят, остальные ключи нового аккаунта в журнал не попадают."""
    async with memory_session(monkeypatch, [TrialIdentityLedger.__table__]) as db:
        await identity.claim_identities(db, _user(google_id='g1'))
        await db.commit()

        with pytest.raises(LiteTrialRefused):
            await identity.claim_identities(db, _user(google_id='g1', telegram_id=99))
        await db.commit()

        assert len(await _rows(db)) == 1


@pytest.mark.asyncio
async def test_unknown_identities_pass_and_do_not_interfere(monkeypatch) -> None:
    async with memory_session(monkeypatch, [TrialIdentityLedger.__table__]) as db:
        await identity.claim_identities(db, _user(google_id='g1'))
        await db.commit()

        await identity.ensure_identities_free(db, _user(google_id='g2', telegram_id=7))
        await identity.claim_identities(db, _user(google_id='g2', telegram_id=7))
        await db.commit()

        assert len(await _rows(db)) == 3


@pytest.mark.asyncio
async def test_an_account_without_any_identity_is_not_blocked_and_records_nothing(monkeypatch) -> None:
    async with memory_session(monkeypatch, [TrialIdentityLedger.__table__]) as db:
        await identity.ensure_identities_free(db, _user())
        await identity.claim_identities(db, _user())
        await identity.claim_identities(db, _user())
        await db.commit()

        assert await _rows(db) == []


@pytest.mark.asyncio
async def test_an_admin_trial_reset_bypasses_the_ledger(monkeypatch) -> None:
    """Сброс триала администратором открывает его заново, даже если личность уже в журнале."""
    from datetime import UTC, datetime

    async with memory_session(monkeypatch, [TrialIdentityLedger.__table__]) as db:
        await identity.claim_identities(db, _user(telegram_id=42))
        await db.commit()
        reopened = _user(telegram_id=42, trial_reset_at=datetime.now(UTC))

        await identity.ensure_identities_free(db, reopened)
        await identity.claim_identities(db, reopened)
        await db.commit()

        assert len(await _rows(db)) == 1


@pytest.mark.asyncio
async def test_a_failed_claim_leaves_the_session_usable(monkeypatch) -> None:
    async with memory_session(monkeypatch, [TrialIdentityLedger.__table__]) as db:
        await identity.claim_identities(db, _user(google_id='g1'))
        await db.commit()

        with pytest.raises(LiteTrialRefused):
            await identity.claim_identities(db, _user(google_id='g1'))

        await identity.claim_identities(db, _user(google_id='g3'))
        await db.commit()

        assert len(await _rows(db)) == 2
