"""Настройки бонуса за привязку второго способа входа: значения по умолчанию, .env.example, правка из админки.

Бонус выключен по умолчанию; все параметры лежат в ``Settings`` с префиксом ``AUTH_LINK_BONUS_``
и редактируются из админки так же, как остальные настройки (с границами для числа дней).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.config import Settings, settings
from app.services.system_settings_service import BotConfigurationService


ENV_EXAMPLE = Path(__file__).resolve().parents[2] / '.env.example'

DEFAULTS: dict[str, object] = {
    'AUTH_LINK_BONUS_ENABLED': False,
    'AUTH_LINK_BONUS_DAYS': 7,
    'AUTH_LINK_BONUS_REQUIRES_PAID': True,
}

BOUNDED = {'AUTH_LINK_BONUS_DAYS': (1, 365)}


@pytest.mark.parametrize(('name', 'expected'), DEFAULTS.items())
def test_defaults(name, expected) -> None:
    assert Settings.model_fields[name].default == expected


def test_bonus_is_off_by_default() -> None:
    assert Settings.model_fields['AUTH_LINK_BONUS_ENABLED'].default is False


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
def test_setting_is_listed_in_one_admin_category(name) -> None:
    BotConfigurationService.initialize_definitions()
    definition = BotConfigurationService.get_definition(name)
    assert definition.category_key == 'REGISTRATION_ACCESS'
    assert name in {d.key for d in BotConfigurationService.get_settings_for_category('REGISTRATION_ACCESS')}


@pytest.mark.parametrize('name', DEFAULTS)
def test_setting_has_an_admin_hint(name) -> None:
    BotConfigurationService.initialize_definitions()
    hint = BotConfigurationService.SETTING_HINTS.get(name)
    assert hint, f'{name}: нет подсказки в SETTING_HINTS'
    assert hint['description'] and hint['format'] and hint['example'] and hint['warning']


@pytest.mark.parametrize(('name', 'bounds'), BOUNDED.items())
def test_days_have_documented_bounds(name, bounds) -> None:
    assert BotConfigurationService.get_bounds(name) == bounds
    low, high = bounds
    assert low <= DEFAULTS[name] <= high
    hint = BotConfigurationService.get_setting_guidance(name)
    assert str(low) in hint['format'] and str(high) in hint['format']


@pytest.mark.parametrize(('name', 'bounds'), BOUNDED.items())
def test_value_outside_the_bounds_is_rejected(name, bounds) -> None:
    low, high = bounds
    with pytest.raises(ValueError):
        BotConfigurationService.parse_user_value(name, str(low - 1))
    with pytest.raises(ValueError):
        BotConfigurationService.parse_user_value(name, str(high + 1))
    assert BotConfigurationService.parse_user_value(name, str(low)) == low
    assert BotConfigurationService.parse_user_value(name, str(high)) == high


def test_a_value_set_from_the_admin_applies_without_restart(monkeypatch) -> None:
    monkeypatch.setattr(settings, 'AUTH_LINK_BONUS_DAYS', 7)
    monkeypatch.setattr(BotConfigurationService, '_env_override_keys', set())

    BotConfigurationService._apply_to_settings('AUTH_LINK_BONUS_DAYS', 14)

    assert settings.AUTH_LINK_BONUS_DAYS == 14


@pytest.mark.parametrize('coerce_path', ['app.cabinet.routes.admin_settings', 'app.webapi.routes.config'])
def test_http_settings_routes_reject_days_outside_the_bounds(coerce_path) -> None:
    import importlib

    from fastapi import HTTPException

    coerce = importlib.import_module(coerce_path)._coerce_value
    for bad in (0, 366):
        with pytest.raises(HTTPException) as refused:
            coerce('AUTH_LINK_BONUS_DAYS', bad)
        assert refused.value.status_code == 400
    assert coerce('AUTH_LINK_BONUS_DAYS', 365) == 365
    assert coerce('AUTH_LINK_BONUS_ENABLED', 'true') is True
