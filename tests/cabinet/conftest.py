"""Общие фикстуры тестов кабинета: Redis в памяти и включённый passkey."""

from __future__ import annotations

import pytest

from app.config import settings
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
