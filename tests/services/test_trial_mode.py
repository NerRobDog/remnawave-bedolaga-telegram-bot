"""Режим триала пользователя: одна функция для кабинета, бота и мини-приложения.

``full`` — обычный триал (за аккаунтом есть Telegram, «якорь»), ``lite`` — короткая проверка
для аккаунта без Telegram, ``None`` — триала нет. Без ``TRIAL_LITE_ENABLED`` режим обязан
совпадать с тем, что решали прежние проверки ``is_trial_disabled_for_user``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.config import settings
from app.services.trial_eligibility import (
    LiteTrialLimits,
    TrialMode,
    get_lite_trial_limits,
    get_trial_mode,
)


AUTH_TYPES = ['telegram', 'email', 'google', 'yandex', 'discord', 'vk', 'passkey']
DISABLED_FOR = ['none', 'email', 'telegram', 'all']


def _user(auth_type: str = 'telegram', telegram_id: int | None = 100) -> SimpleNamespace:
    return SimpleNamespace(auth_type=auth_type, telegram_id=telegram_id)


@pytest.fixture(autouse=True)
def _baseline(monkeypatch):
    monkeypatch.setattr(settings, 'TRIAL_DISABLED_FOR', 'none')
    monkeypatch.setattr(settings, 'TRIAL_LITE_ENABLED', False)
    monkeypatch.setattr(settings, 'PASSKEY_SIGNUP_TRIAL_ENABLED', False)


@pytest.mark.parametrize('disabled_for', DISABLED_FOR)
@pytest.mark.parametrize('auth_type', AUTH_TYPES)
@pytest.mark.parametrize('telegram_id', [100, None])
def test_flag_off_mode_is_exactly_the_old_gate(monkeypatch, disabled_for, auth_type, telegram_id) -> None:
    monkeypatch.setattr(settings, 'TRIAL_DISABLED_FOR', disabled_for)
    expected = None if settings.is_trial_disabled_for_user(auth_type) else TrialMode.FULL

    assert get_trial_mode(_user(auth_type, telegram_id)) == expected


def test_modes_are_plain_strings_for_the_api() -> None:
    assert TrialMode.FULL == 'full'
    assert TrialMode.LITE == 'lite'


@pytest.mark.parametrize('disabled_for', ['none', 'email', 'telegram'])
@pytest.mark.parametrize('auth_type', ['email', 'google', 'yandex', 'discord', 'vk', 'passkey'])
def test_without_telegram_the_account_gets_lite(monkeypatch, disabled_for, auth_type) -> None:
    monkeypatch.setattr(settings, 'TRIAL_LITE_ENABLED', True)
    monkeypatch.setattr(settings, 'TRIAL_DISABLED_FOR', disabled_for)

    assert get_trial_mode(_user(auth_type, telegram_id=None)) == TrialMode.LITE


def test_disabled_for_email_does_not_close_the_lite_trial_but_keeps_the_full_one_closed(monkeypatch) -> None:
    """Прод: TRIAL_DISABLED_FOR=email. Лёгкий триал без Telegram разрешён, полный — по-прежнему нет."""
    monkeypatch.setattr(settings, 'TRIAL_DISABLED_FOR', 'email')
    assert get_trial_mode(_user('google', telegram_id=None)) is None

    monkeypatch.setattr(settings, 'TRIAL_LITE_ENABLED', True)
    assert get_trial_mode(_user('google', telegram_id=None)) == TrialMode.LITE
    assert get_trial_mode(_user('email', telegram_id=100)) is None


@pytest.mark.parametrize('auth_type', AUTH_TYPES)
def test_disabled_for_all_closes_every_mode(monkeypatch, auth_type) -> None:
    monkeypatch.setattr(settings, 'TRIAL_LITE_ENABLED', True)
    monkeypatch.setattr(settings, 'TRIAL_DISABLED_FOR', 'all')

    assert get_trial_mode(_user(auth_type, telegram_id=None)) is None
    assert get_trial_mode(_user(auth_type, telegram_id=100)) is None


@pytest.mark.parametrize('disabled_for', DISABLED_FOR)
@pytest.mark.parametrize('auth_type', AUTH_TYPES)
def test_with_telegram_the_full_rules_apply_even_when_lite_is_on(monkeypatch, disabled_for, auth_type) -> None:
    monkeypatch.setattr(settings, 'TRIAL_LITE_ENABLED', True)
    monkeypatch.setattr(settings, 'TRIAL_DISABLED_FOR', disabled_for)
    expected = None if settings.is_trial_disabled_for_user(auth_type) else TrialMode.FULL

    assert get_trial_mode(_user(auth_type, telegram_id=100)) == expected


def test_passkey_signup_trial_flag_still_governs_the_full_trial_only(monkeypatch) -> None:
    monkeypatch.setattr(settings, 'TRIAL_LITE_ENABLED', True)
    assert get_trial_mode(_user('passkey', telegram_id=100)) is None

    monkeypatch.setattr(settings, 'PASSKEY_SIGNUP_TRIAL_ENABLED', True)
    assert get_trial_mode(_user('passkey', telegram_id=100)) == TrialMode.FULL


def test_user_without_attributes_is_treated_like_a_telegram_user() -> None:
    assert get_trial_mode(SimpleNamespace()) == TrialMode.FULL


def test_limits_follow_the_settings_live(monkeypatch) -> None:
    monkeypatch.setattr(settings, 'TRIAL_LITE_DURATION_HOURS', 6)
    monkeypatch.setattr(settings, 'TRIAL_LITE_TRAFFIC_LIMIT_GB', 5)
    monkeypatch.setattr(settings, 'TRIAL_LITE_DEVICE_LIMIT', 2)
    monkeypatch.setattr(settings, 'TRIAL_LITE_DAILY_LIMIT', 10)
    monkeypatch.setattr(settings, 'TRIAL_LITE_PER_IP_DAILY_LIMIT', 3)

    assert get_lite_trial_limits() == LiteTrialLimits(
        duration_hours=6, traffic_limit_gb=5, device_limit=2, daily_limit=10, per_ip_daily_limit=3
    )


def test_limits_from_the_environment_are_clamped_to_the_admin_bounds(monkeypatch) -> None:
    """Значение из .env не проходит валидацию админки, поэтому границы применяются при чтении."""
    monkeypatch.setattr(settings, 'TRIAL_LITE_DURATION_HOURS', 0)
    monkeypatch.setattr(settings, 'TRIAL_LITE_TRAFFIC_LIMIT_GB', 0)
    monkeypatch.setattr(settings, 'TRIAL_LITE_DEVICE_LIMIT', 500)
    monkeypatch.setattr(settings, 'TRIAL_LITE_DAILY_LIMIT', -5)
    monkeypatch.setattr(settings, 'TRIAL_LITE_PER_IP_DAILY_LIMIT', 10**9)

    limits = get_lite_trial_limits()

    assert limits.duration_hours == 1
    assert limits.traffic_limit_gb == 1, '0 ГБ в панели значит безлимит: для лёгкого триала недопустимо'
    assert limits.device_limit == 20
    assert limits.daily_limit == 0
    assert limits.per_ip_daily_limit == 1000
