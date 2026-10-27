"""Настройки passkey: включено только при полной конфигурации."""

from __future__ import annotations

import pytest

from app.config import Settings


def _settings(**overrides) -> Settings:
    base = dict(PASSKEY_ENABLED=True, PASSKEY_RP_ID='watchd0g.dev', PASSKEY_ORIGINS='https://my.watchd0g.dev')
    base.update(overrides)
    return Settings(**base)


def test_defaults_keep_passkey_off() -> None:
    fresh = Settings()

    assert fresh.PASSKEY_ENABLED is False
    assert fresh.PASSKEY_RP_ID == ''
    assert fresh.PASSKEY_RP_NAME == 'Watchdog'
    assert fresh.PASSKEY_ORIGINS == ''
    assert fresh.is_passkey_enabled() is False


def test_enabled_with_rp_id_and_origin() -> None:
    assert _settings().is_passkey_enabled() is True


@pytest.mark.parametrize(
    'overrides',
    [
        {'PASSKEY_ENABLED': False},
        {'PASSKEY_RP_ID': ''},
        {'PASSKEY_RP_ID': '   '},
        {'PASSKEY_ORIGINS': ''},
        {'PASSKEY_ORIGINS': ' , ,'},
    ],
)
def test_incomplete_configuration_stays_disabled(overrides) -> None:
    assert _settings(**overrides).is_passkey_enabled() is False


def test_origins_are_split_trimmed_and_stripped_of_trailing_slash() -> None:
    parsed = _settings(PASSKEY_ORIGINS=' https://my.watchd0g.dev/ , https://watchd0g.dev ,, ').get_passkey_origins()

    assert parsed == ['https://my.watchd0g.dev', 'https://watchd0g.dev']


def test_rp_id_is_trimmed_and_lowercased() -> None:
    assert _settings(PASSKEY_RP_ID=' WatchD0g.dev ').get_passkey_rp_id() == 'watchd0g.dev'
