"""Ссылка возврата на инвойсе CryptoBot из Telegram-бота."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.config import settings
from app.handlers.balance.cryptobot import _resolve_bot_return_url


@pytest.fixture
def anyio_backend() -> str:
    return 'asyncio'


@pytest.mark.anyio('asyncio')
async def test_return_url_uses_configured_username(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'BOT_USERNAME', '@my_bot', raising=False)

    class Bot:
        async def get_me(self):  # pragma: no cover - не должен вызываться
            raise AssertionError('get_me must not be called when BOT_USERNAME is set')

    assert await _resolve_bot_return_url(Bot()) == 'https://t.me/my_bot'


@pytest.mark.anyio('asyncio')
async def test_return_url_falls_back_to_running_bot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'BOT_USERNAME', None, raising=False)

    class Bot:
        async def get_me(self):
            return SimpleNamespace(username='running_bot')

    assert await _resolve_bot_return_url(Bot()) == 'https://t.me/running_bot'


@pytest.mark.anyio('asyncio')
async def test_return_url_is_none_when_username_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'BOT_USERNAME', None, raising=False)

    class Bot:
        async def get_me(self):
            raise RuntimeError('telegram is down')

    assert await _resolve_bot_return_url(Bot()) is None
