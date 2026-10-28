"""Бот, мини-приложение и rich-меню решают «положен ли полный триал» тем же get_trial_mode, что и кабинет.

Лёгкий триал выдаёт только кабинет, поэтому на остальных поверхностях триал открыт ровно тогда,
когда режим пользователя — ``full``. Без ``TRIAL_LITE_ENABLED`` результат совпадает с прежним
``is_trial_disabled_for_user`` (отдельный тест матрицы ниже).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import settings
from app.handlers import start as start_handler
from app.handlers.subscription import purchase
from app.services.trial_eligibility import TrialMode, get_trial_mode
from app.utils import rich_menu
from app.webapi.routes import miniapp


ACCOUNTS = [
    ('telegram', 100),
    ('email', 100),
    ('google', 100),
    ('google', None),
    ('email', None),
    ('passkey', None),
    ('passkey', 100),
]


def _user(auth_type: str, telegram_id: int | None) -> SimpleNamespace:
    return SimpleNamespace(
        id=1,
        language='ru',
        auth_type=auth_type,
        telegram_id=telegram_id,
        has_had_paid_subscription=False,
        subscriptions=[],
        subscription=None,
        restriction_subscription=False,
        is_trial_already_used=MagicMock(return_value=False),
    )


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setattr(settings, 'TRIAL_DURATION_DAYS', 3)
    monkeypatch.setattr(settings, 'PASSKEY_SIGNUP_TRIAL_ENABLED', True)
    monkeypatch.setattr(type(settings), 'is_trial_paid_activation_enabled', lambda self: False)
    monkeypatch.setattr(type(settings), 'get_bot_username', lambda self: 'testbot')


# -- поверхности -------------------------------------------------------------


def _miniapp_open(user) -> bool:
    return miniapp._is_trial_available_for_user(user)


def _rich_menu_open(user) -> bool:
    return bool(rich_menu._trial_offer_link(user, SimpleNamespace(t=lambda key, default, **kw: default)))


async def _bot_offer_open(user) -> bool:
    callback = AsyncMock()
    callback.message = AsyncMock()
    await purchase.show_trial_offer(callback, user, AsyncMock())
    return user.is_trial_already_used.called


async def _bot_activate_open(user) -> bool:
    callback = AsyncMock()
    callback.message = AsyncMock()
    await purchase.activate_trial(callback, user, AsyncMock())
    return user.is_trial_already_used.called


async def _deeplink_open(monkeypatch, user) -> bool:
    probed = AsyncMock(side_effect=RuntimeError('gate passed'))
    monkeypatch.setattr('app.database.crud.tariff.get_trial_tariff', probed)
    state = AsyncMock()
    state.get_data = AsyncMock(return_value={'pending_trial': True})
    await start_handler._activate_pending_trial(AsyncMock(), state, user, AsyncMock())
    return probed.called


async def _surfaces(monkeypatch, user) -> dict[str, bool]:
    return {
        'miniapp': _miniapp_open(user),
        'rich_menu': _rich_menu_open(user),
        'bot_offer': await _bot_offer_open(user),
        'bot_activate': await _bot_activate_open(_fresh(user)),
        'start_deeplink': await _deeplink_open(monkeypatch, _fresh(user)),
    }


def _fresh(user) -> SimpleNamespace:
    return _user(user.auth_type, user.telegram_id)


@pytest.mark.asyncio
@pytest.mark.parametrize('lite_enabled', [False, True])
@pytest.mark.parametrize('disabled_for', ['none', 'email', 'telegram', 'all'])
@pytest.mark.parametrize(('auth_type', 'telegram_id'), ACCOUNTS)
async def test_every_surface_follows_the_shared_mode(
    monkeypatch, lite_enabled, disabled_for, auth_type, telegram_id
) -> None:
    monkeypatch.setattr(settings, 'TRIAL_LITE_ENABLED', lite_enabled)
    monkeypatch.setattr(settings, 'TRIAL_DISABLED_FOR', disabled_for)
    user = _user(auth_type, telegram_id)
    expected = get_trial_mode(user) == TrialMode.FULL

    surfaces = await _surfaces(monkeypatch, user)

    assert surfaces == dict.fromkeys(surfaces, expected), f'режим={get_trial_mode(user)}'


@pytest.mark.asyncio
@pytest.mark.parametrize('disabled_for', ['none', 'email', 'telegram', 'all'])
@pytest.mark.parametrize(('auth_type', 'telegram_id'), ACCOUNTS)
async def test_with_the_flag_off_the_surfaces_keep_the_old_decision(
    monkeypatch, disabled_for, auth_type, telegram_id
) -> None:
    monkeypatch.setattr(settings, 'TRIAL_LITE_ENABLED', False)
    monkeypatch.setattr(settings, 'TRIAL_DISABLED_FOR', disabled_for)
    old_decision_open = not settings.is_trial_disabled_for_user(auth_type)

    surfaces = await _surfaces(monkeypatch, _user(auth_type, telegram_id))

    assert surfaces == dict.fromkeys(surfaces, old_decision_open)


@pytest.mark.asyncio
async def test_a_zero_trial_duration_still_closes_the_full_trial_everywhere(monkeypatch) -> None:
    monkeypatch.setattr(settings, 'TRIAL_LITE_ENABLED', True)
    monkeypatch.setattr(settings, 'TRIAL_DISABLED_FOR', 'none')
    monkeypatch.setattr(settings, 'TRIAL_DURATION_DAYS', 0)

    surfaces = await _surfaces(monkeypatch, _user('telegram', 100))

    assert surfaces == dict.fromkeys(surfaces, False)
