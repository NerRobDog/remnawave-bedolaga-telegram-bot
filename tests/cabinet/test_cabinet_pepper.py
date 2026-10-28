"""CABINET_PEPPER: отдельный перец для хэшей recovery-кодов и журнала личностей триала.

Главное требование — обратная совместимость: пока ``CABINET_PEPPER`` не задан, все хэши
байт-в-байт совпадают с теми, что считались от ``CABINET_JWT_SECRET`` (а без него — от
``BOT_TOKEN``). Значения ниже посчитаны старым кодом до появления настройки.
"""

from __future__ import annotations

import warnings

import pytest

from app.cabinet.auth import recovery_codes
from app.cabinet.auth.pepper import pepper_secret
from app.config import settings
from app.services import trial_identity
from app.services.system_settings_service import BotConfigurationService


JWT_SECRET = 'pinned-jwt-secret-for-hash-tests-0123456789'
BOT_TOKEN = '123:abc'

#: Посчитано кодом до введения CABINET_PEPPER, при CABINET_JWT_SECRET = JWT_SECRET.
PINNED_FROM_JWT = {
    'code:ABCDEFGHJKMNPQRS': 'd43d6260db409b5dd622a88f59eca8a83c8767783ea6ca74ee3e5510613ab256',
    'code:0123456789ABCDEF': '2f80715a03e21d2ac19e0355c34581ca7d5adeb56adeefd3bdda90aeb133bba6',
    'tg:42': '5b6c158ba7f3529eee5b8d222b762590130c5dcf2d845120423f8c74bab56189',
    'email:john@example.com': '13fb3bd35fa3abb3a394a47baa53eb34b03d930c7448e3a348d11b063339c4f7',
    'google:g1': '0dc425ab4a305d75782f4608c57050d45bc58b19e01511cf571217f912ebe6c8',
}
#: То же при пустом CABINET_JWT_SECRET и BOT_TOKEN = '123:abc' (прежний запасной путь).
PINNED_FROM_BOT_TOKEN = {
    'code:ABCDEFGHJKMNPQRS': '09b5a758120c6a9381d97bc0add85429546903e364cb147df4f35513e19632de',
    'tg:42': '2067332071a4b6166ed43145621ba9601e372d8898543c086229bc7c9af8c5ac',
}


def _hash(name: str) -> str:
    kind, _, value = name.partition(':')
    if kind == 'code':
        return recovery_codes.hash_code(value)
    return trial_identity.hash_identity(name)


@pytest.fixture
def jwt_only(monkeypatch):
    monkeypatch.setattr(settings, 'CABINET_JWT_SECRET', JWT_SECRET)
    monkeypatch.setattr(settings, 'CABINET_PEPPER', None)


@pytest.mark.parametrize(('name', 'expected'), PINNED_FROM_JWT.items())
def test_without_cabinet_pepper_hashes_are_byte_identical_to_the_old_ones(jwt_only, name, expected) -> None:
    assert _hash(name) == expected


@pytest.mark.parametrize(('name', 'expected'), PINNED_FROM_BOT_TOKEN.items())
def test_the_bot_token_fallback_is_unchanged(monkeypatch, name, expected) -> None:
    monkeypatch.setattr(settings, 'CABINET_JWT_SECRET', '')
    monkeypatch.setattr(settings, 'CABINET_PEPPER', None)
    monkeypatch.setattr(settings, 'BOT_TOKEN', BOT_TOKEN)

    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        assert _hash(name) == expected


def test_the_bot_token_fallback_still_warns(monkeypatch) -> None:
    monkeypatch.setattr(settings, 'CABINET_JWT_SECRET', '')
    monkeypatch.setattr(settings, 'CABINET_PEPPER', None)

    with pytest.warns(UserWarning, match='CABINET_JWT_SECRET is not set'):
        pepper_secret()


@pytest.mark.parametrize('name', PINNED_FROM_JWT)
def test_cabinet_pepper_changes_every_hash(monkeypatch, jwt_only, name) -> None:
    monkeypatch.setattr(settings, 'CABINET_PEPPER', 'a-dedicated-pepper-value-9876543210')

    assert _hash(name) != PINNED_FROM_JWT[name]


@pytest.mark.parametrize('name', PINNED_FROM_JWT)
def test_rotating_the_jwt_secret_does_not_move_hashes_once_the_pepper_is_set(monkeypatch, jwt_only, name) -> None:
    monkeypatch.setattr(settings, 'CABINET_PEPPER', 'a-dedicated-pepper-value-9876543210')
    before = _hash(name)

    monkeypatch.setattr(settings, 'CABINET_JWT_SECRET', 'a-completely-new-jwt-secret-after-rotation')

    assert _hash(name) == before


@pytest.mark.parametrize('name', PINNED_FROM_JWT)
def test_setting_the_pepper_to_the_current_jwt_secret_keeps_every_hash(monkeypatch, jwt_only, name) -> None:
    """Путь миграции: перед ротацией CABINET_PEPPER = текущий CABINET_JWT_SECRET, ничего не теряется."""
    monkeypatch.setattr(settings, 'CABINET_PEPPER', JWT_SECRET)

    assert _hash(name) == PINNED_FROM_JWT[name]


def test_changing_the_pepper_changes_the_hashes(monkeypatch, jwt_only) -> None:
    monkeypatch.setattr(settings, 'CABINET_PEPPER', 'first-pepper-value-0123456789')
    first = _hash('tg:42')

    monkeypatch.setattr(settings, 'CABINET_PEPPER', 'second-pepper-value-0123456789')

    assert _hash('tg:42') != first


def test_precedence_is_pepper_then_jwt_secret_then_bot_token(monkeypatch) -> None:
    monkeypatch.setattr(settings, 'BOT_TOKEN', BOT_TOKEN)
    monkeypatch.setattr(settings, 'CABINET_JWT_SECRET', 'jwt')
    monkeypatch.setattr(settings, 'CABINET_PEPPER', 'pepper')
    assert pepper_secret() == b'pepper'

    monkeypatch.setattr(settings, 'CABINET_PEPPER', None)
    assert pepper_secret() == b'jwt'

    monkeypatch.setattr(settings, 'CABINET_JWT_SECRET', '')
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        assert pepper_secret() == BOT_TOKEN.encode()


@pytest.mark.parametrize('blank', ['', '   '])
def test_a_blank_pepper_counts_as_unset(monkeypatch, blank) -> None:
    monkeypatch.setattr(settings, 'CABINET_JWT_SECRET', 'jwt')
    monkeypatch.setattr(settings, 'CABINET_PEPPER', blank)

    assert pepper_secret() == b'jwt'


def test_the_two_users_keep_different_labels_under_one_pepper(monkeypatch, jwt_only) -> None:
    """Один перец, но домены хэшей разделены метками: код и ключ личности с одним текстом не совпадают."""
    monkeypatch.setattr(settings, 'CABINET_PEPPER', 'shared-pepper-value-0123456789')

    assert recovery_codes.hash_code('ABCDEFGHJKMNPQRS') != trial_identity.hash_identity('ABCDEFGHJKMNPQRS')


def test_the_pepper_is_not_an_admin_editable_setting() -> None:
    """Секрет: ни в списке настроек админки, ни через API настроек."""
    BotConfigurationService.initialize_definitions()

    assert 'CABINET_PEPPER' in BotConfigurationService.EXCLUDED_KEYS
    assert 'CABINET_PEPPER' not in {d.key for d in BotConfigurationService._definitions.values()}


def test_the_pepper_is_optional_and_documented() -> None:
    from pathlib import Path

    from app.config import Settings

    assert Settings.model_fields['CABINET_PEPPER'].default is None
    example = (Path(__file__).resolve().parents[2] / '.env.example').read_text(encoding='utf-8')
    assert '#CABINET_PEPPER=' in example, 'в .env.example перец закомментирован: по умолчанию не задан'
