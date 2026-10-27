"""Recovery-коды: формат, нормализация ввода, хэш с перцем. Без БД и HTTP."""

from __future__ import annotations

import re

import pytest

from app.cabinet.auth import recovery_codes
from app.config import settings


ALPHABET = set('0123456789ABCDEFGHJKMNPQRSTVWXYZ')
SHOWN = re.compile(r'[0-9A-Z]{4}(-[0-9A-Z]{4}){3}')


@pytest.fixture(autouse=True)
def _secret(monkeypatch) -> None:
    monkeypatch.setattr(settings, 'CABINET_JWT_SECRET', 'recovery-tests-' + 'k' * 48)


def test_a_code_is_80_bits_of_crockford_base32() -> None:
    code = recovery_codes.generate_code()

    assert len(code) == 16
    assert set(code) <= ALPHABET
    assert 16 * 5 == 80


def test_codes_do_not_repeat() -> None:
    assert len({recovery_codes.generate_code() for _ in range(2000)}) == 2000


def test_a_code_is_shown_in_four_groups_of_four() -> None:
    shown = recovery_codes.format_code('0123456789ABCDEF')

    assert shown == '0123-4567-89AB-CDEF'
    assert SHOWN.fullmatch(shown)


def test_a_fresh_set_has_eight_distinct_codes_and_hashes() -> None:
    shown, hashes = zip(*recovery_codes.new_code_set(), strict=True)

    assert len(shown) == len(hashes) == recovery_codes.CODES_PER_SET == 8
    assert len(set(shown)) == len(set(hashes)) == 8
    assert all(SHOWN.fullmatch(code) for code in shown)


@pytest.mark.parametrize(
    'typed',
    [
        '0123-4567-89AB-CDEF',
        '0123456789ABCDEF',
        '  0123-4567-89ab-cdef  ',
        '0123 4567 89ab cdef',
        '0123_4567_89AB_CDEF',
        'O123-4567-89AB-CDEF',  # O читается как 0
        'o123-4567-89ab-cdef',
    ],
)
def test_human_spellings_of_a_code_normalise_to_the_same_value(typed) -> None:
    assert recovery_codes.normalize_code(typed) == '0123456789ABCDEF'


@pytest.mark.parametrize('typed', ['I123-4567-89AB-CDEF', 'l123-4567-89AB-CDEF', '1123-4567-89AB-CDEF'])
def test_i_and_l_are_read_as_one(typed) -> None:
    assert recovery_codes.normalize_code(typed) == '1123456789ABCDEF'


@pytest.mark.parametrize(
    'junk',
    [
        '',
        '   ',
        '0123-4567-89AB-CDE',  # короче
        '0123-4567-89AB-CDEFG',  # длиннее
        '0123-4567-89AB-CDEU',  # U нет в алфавите
        '0123-4567-89AB-CDE!',
        '0123-4567-89AB-CDEЖ',
        '\x00' * 16,
        'A' * 10_000,
        None,
        123,
        b'0123456789ABCDEF',
    ],
)
def test_anything_else_is_not_a_code(junk) -> None:
    assert recovery_codes.normalize_code(junk) is None


def test_the_hash_is_hex_sha256_sized_and_never_contains_the_code() -> None:
    code = recovery_codes.generate_code()
    digest = recovery_codes.hash_code(code)

    assert re.fullmatch(r'[0-9a-f]{64}', digest)
    assert code.lower() not in digest and code not in digest


def test_the_hash_is_stable_and_one_per_code() -> None:
    assert recovery_codes.hash_code('0123456789ABCDEF') == recovery_codes.hash_code('0123456789ABCDEF')
    assert recovery_codes.hash_code('0123456789ABCDEF') != recovery_codes.hash_code('0123456789ABCDEG')


def test_the_hash_is_keyed_by_the_server_secret(monkeypatch) -> None:
    """Утечка таблицы без секрета не даёт перебирать коды офлайн."""
    before = recovery_codes.hash_code('0123456789ABCDEF')
    monkeypatch.setattr(settings, 'CABINET_JWT_SECRET', 'another-secret-' + 'x' * 48)

    assert recovery_codes.hash_code('0123456789ABCDEF') != before


def test_hash_of_typed_input_matches_hash_of_the_stored_code() -> None:
    shown, stored = recovery_codes.new_code_set()[0]

    assert recovery_codes.hash_typed_code(shown.lower()) == stored
    assert recovery_codes.hash_typed_code(shown.replace('-', '')) == stored


def test_hash_of_typed_junk_is_none() -> None:
    assert recovery_codes.hash_typed_code('not a code') is None
