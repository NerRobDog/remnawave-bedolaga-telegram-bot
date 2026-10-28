"""Режим триала пользователя и параметры лёгкого триала.

Одна функция решает, какой триал положен аккаунту, — её зовут кабинет, бот и мини-приложение,
чтобы они не расходились:

* ``full`` — обычный триал. Положен аккаунту с Telegram («якорь»); правила прежние
  (``TRIAL_DISABLED_FOR`` и ``PASSKEY_SIGNUP_TRIAL_ENABLED`` через ``is_trial_disabled_for_user``).
* ``lite`` — короткая проверка с потолком расходов для аккаунта без Telegram. Только при
  ``TRIAL_LITE_ENABLED``; ``TRIAL_DISABLED_FOR=email`` закрывает полный триал таким аккаунтам,
  но не лёгкий.
* ``None`` — триала нет (``TRIAL_DISABLED_FOR=all`` закрывает оба режима).

Функция смотрит только на настройки и якорь аккаунта. Использован ли триал уже, решает
``User.is_trial_already_used`` — вызывающий код по-прежнему проверяет это сам.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from app.config import settings
from app.services.system_settings_service import BotConfigurationService


class TrialMode(StrEnum):
    FULL = 'full'
    LITE = 'lite'


_UNSET: Any = object()


def get_trial_mode(user: Any) -> TrialMode | None:
    """Какой триал положен аккаунту по настройкам и наличию Telegram; ``None`` — никакого."""
    if settings.TRIAL_DISABLED_FOR == 'all':
        return None

    # Объект без поля telegram_id (внешние вызовы, заглушки) считается привязанным к Telegram:
    # так он и вёл себя в прежних проверках.
    has_anchor = getattr(user, 'telegram_id', _UNSET) is not None
    if has_anchor or not settings.TRIAL_LITE_ENABLED:
        if settings.is_trial_disabled_for_user(getattr(user, 'auth_type', None)):
            return None
        return TrialMode.FULL
    return TrialMode.LITE


@dataclass(frozen=True)
class LiteTrialLimits:
    duration_hours: int
    traffic_limit_gb: int
    device_limit: int
    daily_limit: int
    per_ip_daily_limit: int


def _clamped(key: str) -> int:
    low, high = BotConfigurationService.NUMERIC_BOUNDS[key]
    return max(low, min(high, int(getattr(settings, key))))


def get_lite_trial_limits() -> LiteTrialLimits:
    """Параметры лёгкого триала из настроек (читаются при каждом вызове — правка из админки живая).

    Значение из ``.env`` не проходит проверку границ админки, поэтому ограничивается здесь:
    0 ГБ в панели означало бы безлимит, а лёгкий триал безлимитным быть не может.
    """
    return LiteTrialLimits(
        duration_hours=_clamped('TRIAL_LITE_DURATION_HOURS'),
        traffic_limit_gb=_clamped('TRIAL_LITE_TRAFFIC_LIMIT_GB'),
        device_limit=_clamped('TRIAL_LITE_DEVICE_LIMIT'),
        daily_limit=_clamped('TRIAL_LITE_DAILY_LIMIT'),
        per_ip_daily_limit=_clamped('TRIAL_LITE_PER_IP_DAILY_LIMIT'),
    )
