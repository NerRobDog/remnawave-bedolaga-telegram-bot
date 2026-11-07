"""Настройки лёгкого триала: значения по умолчанию, .env.example и правка из админки без перезапуска.

Лёгкий триал выключен по умолчанию; все его параметры лежат в ``Settings`` с префиксом
``TRIAL_LITE_``, а значит попадают в категорию «Пробный период» и редактируются так же,
как остальные ``TRIAL_*``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.config import Settings, settings
from app.services.system_settings_service import BotConfigurationService


ENV_EXAMPLE = Path(__file__).resolve().parents[2] / '.env.example'

DEFAULTS: dict[str, object] = {
    'TRIAL_LITE_ENABLED': False,
    'TRIAL_LITE_DURATION_HOURS': 24,
    'TRIAL_LITE_TRAFFIC_LIMIT_GB': 2,
    'TRIAL_LITE_DEVICE_LIMIT': 1,
    'TRIAL_LITE_DAILY_LIMIT': 30,
    'TRIAL_LITE_PER_IP_DAILY_LIMIT': 1,
}

BOUNDED = {k: v for k, v in DEFAULTS.items() if not isinstance(v, bool)}


@pytest.mark.parametrize(('name', 'expected'), DEFAULTS.items())
def test_defaults(name, expected) -> None:
    assert Settings.model_fields[name].default == expected


def test_lite_trial_is_off_by_default() -> None:
    assert Settings.model_fields['TRIAL_LITE_ENABLED'].default is False


def _env_example_value(name: str) -> str:
    match = re.search(rf'^#?\s*{name}=(.*)$', ENV_EXAMPLE.read_text(encoding='utf-8'), re.MULTILINE)
    assert match, f'{name} отсутствует в .env.example'
    return match.group(1).strip()


@pytest.mark.parametrize('name', DEFAULTS)
def test_env_example_documents_the_code_default(name) -> None:
    field = Settings.model_fields[name]
    raw = _env_example_value(name)
    expected = raw.lower() in {'1', 'true', 'yes', 'on'} if field.annotation is bool else field.annotation(raw)
    assert field.default == expected


@pytest.mark.parametrize('name', DEFAULTS)
def test_setting_is_listed_in_the_trial_category(name) -> None:
    BotConfigurationService.initialize_definitions()
    definition = BotConfigurationService.get_definition(name)
    assert definition.category_key == 'TRIAL'
    assert name in {d.key for d in BotConfigurationService.get_settings_for_category('TRIAL')}


@pytest.mark.parametrize('name', DEFAULTS)
def test_setting_has_an_admin_hint(name) -> None:
    BotConfigurationService.initialize_definitions()
    hint = BotConfigurationService.SETTING_HINTS.get(name)
    assert hint, f'{name}: нет подсказки в SETTING_HINTS'
    assert hint['description'] and hint['format'] and hint['example'] and hint['warning']


@pytest.mark.parametrize('name', BOUNDED)
def test_numeric_setting_has_documented_bounds(name) -> None:
    low, high = BotConfigurationService.get_bounds(name)
    assert low <= BOUNDED[name] <= high
    hint = BotConfigurationService.get_setting_guidance(name)
    assert str(low) in hint['format'] and str(high) in hint['format']


@pytest.mark.parametrize('name', BOUNDED)
def test_value_outside_the_bounds_is_rejected(name) -> None:
    low, high = BotConfigurationService.get_bounds(name)
    with pytest.raises(ValueError):
        BotConfigurationService.parse_user_value(name, str(low - 1))
    with pytest.raises(ValueError):
        BotConfigurationService.parse_user_value(name, str(high + 1))
    assert BotConfigurationService.parse_user_value(name, str(low)) == low
    assert BotConfigurationService.parse_user_value(name, str(high)) == high


def test_a_value_set_from_the_admin_applies_without_restart(monkeypatch) -> None:
    monkeypatch.setattr(settings, 'TRIAL_LITE_DAILY_LIMIT', 30)
    monkeypatch.setattr(BotConfigurationService, '_env_override_keys', set())

    BotConfigurationService._apply_to_settings('TRIAL_LITE_DAILY_LIMIT', 12)

    assert settings.TRIAL_LITE_DAILY_LIMIT == 12


def test_bounds_are_not_invented_for_other_settings() -> None:
    assert BotConfigurationService.get_bounds('TRIAL_DURATION_DAYS') is None


def _coercers():
    from app.cabinet.routes import admin_settings
    from app.webapi.routes import config as web_config

    return [admin_settings._coerce_value, web_config._coerce_value]


@pytest.mark.parametrize('coerce', _coercers(), ids=['cabinet', 'web_api'])
def test_http_settings_routes_reject_a_value_outside_the_bounds(coerce) -> None:
    from fastapi import HTTPException

    low, high = BotConfigurationService.get_bounds('TRIAL_LITE_DAILY_LIMIT')
    for bad in (low - 1, high + 1):
        with pytest.raises(HTTPException) as refused:
            coerce('TRIAL_LITE_DAILY_LIMIT', bad)
        assert refused.value.status_code == 400
    assert coerce('TRIAL_LITE_DAILY_LIMIT', high) == high
    assert coerce('TRIAL_LITE_ENABLED', 'true') is True
