"""Общие фикстуры тестов кабинета: Redis в памяти и включённый passkey."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.services.event_emitter import event_emitter
from app.utils.cache import RateLimitCache, cache
from tests.fixtures.fake_redis import FakeRedis


PASSKEY_RP_ID = 'watchd0g.dev'
PASSKEY_ORIGIN = 'https://my.watchd0g.dev'


@pytest.fixture
def fake_redis(monkeypatch) -> FakeRedis:
    """Подключает ``cache`` и ``RateLimitCache`` к Redis в памяти (код приложения настоящий)."""
    redis = FakeRedis()
    monkeypatch.setattr(cache, 'redis_client', redis)
    monkeypatch.setattr(cache, '_connected', True)
    monkeypatch.setattr(RateLimitCache, '_rate_limit_sha', None)
    return redis


@pytest.fixture
def passkey_settings(monkeypatch) -> None:
    """Passkey включён для rp_id watchd0g.dev и одного origin кабинета."""
    monkeypatch.setattr(settings, 'PASSKEY_ENABLED', True)
    monkeypatch.setattr(settings, 'PASSKEY_RP_ID', PASSKEY_RP_ID)
    monkeypatch.setattr(settings, 'PASSKEY_RP_NAME', 'Watchdog')
    monkeypatch.setattr(settings, 'PASSKEY_ORIGINS', PASSKEY_ORIGIN)
    # Подпись JWT настоящая; секрет длинный, чтобы PyJWT не шумел предупреждениями о слабом ключе.
    monkeypatch.setattr(settings, 'CABINET_JWT_SECRET', 'passkey-tests-' + 'k' * 48)


@pytest.fixture
def passkey_signup_settings(monkeypatch) -> AsyncMock:
    """Регистрация аккаунта одним passkey открыта; согласие с документами не требуется.

    Возвращает перехваченный ``event_emitter.emit``: события не уходят в webhook-таблицы,
    а тест видит, что и когда было отправлено.
    """
    monkeypatch.setattr(settings, 'PASSKEY_SIGNUP_ENABLED', True)
    monkeypatch.setattr(settings, 'CABINET_REQUIRE_LEGAL_CONSENT', False)
    monkeypatch.setattr(settings, 'INVITE_ONLY_ENABLED', False)
    emitted = AsyncMock()
    monkeypatch.setattr(event_emitter, 'emit', emitted)
    return emitted
