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


# --- passkey-only регистрация: настройки и триал ------------------------------


def test_signup_defaults_are_closed_and_free_of_bonuses() -> None:
    fresh = Settings()

    assert fresh.PASSKEY_SIGNUP_ENABLED is False
    assert fresh.PASSKEY_SIGNUP_TRIAL_ENABLED is False
    assert fresh.PASSKEY_SIGNUP_BONUSES_ENABLED is False
    assert (
        fresh.PASSKEY_SIGNUP_LIMIT_PER_MINUTE,
        fresh.PASSKEY_SIGNUP_LIMIT_PER_HOUR,
        fresh.PASSKEY_SIGNUP_LIMIT_PER_DAY,
    ) == (3, 10, 30)


def test_signup_needs_the_whole_passkey_configuration() -> None:
    assert _settings(PASSKEY_SIGNUP_ENABLED=True).is_passkey_signup_enabled() is True
    assert _settings(PASSKEY_SIGNUP_ENABLED=False).is_passkey_signup_enabled() is False
    assert _settings(PASSKEY_SIGNUP_ENABLED=True, PASSKEY_ENABLED=False).is_passkey_signup_enabled() is False
    assert _settings(PASSKEY_SIGNUP_ENABLED=True, PASSKEY_RP_ID='').is_passkey_signup_enabled() is False


@pytest.mark.parametrize(
    ('disabled_for', 'signup_trial', 'expected'),
    [
        # Дыра, которую закрываем: под «none» passkey-аккаунт получил бы триал за секунды.
        ('none', False, True),
        ('none', True, False),
        ('email', False, True),
        ('email', True, True),
        ('telegram', False, True),
        ('telegram', True, False),
        ('all', False, True),
        ('all', True, True),
    ],
)
def test_passkey_accounts_get_no_trial_unless_the_signup_flag_allows_it(disabled_for, signup_trial, expected) -> None:
    configured = _settings(TRIAL_DISABLED_FOR=disabled_for, PASSKEY_SIGNUP_TRIAL_ENABLED=signup_trial)

    assert configured.is_trial_disabled_for_user('passkey') is expected


@pytest.mark.parametrize('auth_type', [None, 'telegram', 'email', 'google'])
@pytest.mark.parametrize('signup_trial', [False, True])
def test_signup_trial_flag_changes_nothing_for_other_auth_types(auth_type, signup_trial) -> None:
    """Флаг касается только auth_type='passkey': остальные ведут себя как до него."""
    baseline = _settings(TRIAL_DISABLED_FOR='none', PASSKEY_SIGNUP_TRIAL_ENABLED=signup_trial)

    assert baseline.is_trial_disabled_for_user(auth_type) is False
    assert _settings(TRIAL_DISABLED_FOR='email').is_trial_disabled_for_user(auth_type) is (
        auth_type not in (None, 'telegram')
    )
