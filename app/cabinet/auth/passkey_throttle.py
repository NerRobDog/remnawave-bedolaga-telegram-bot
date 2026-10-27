"""Дроссели passkey-регистрации и входа по recovery-коду (по IP, три окна).

Одного минутного окна мало: скрипт ждёт минуту и продолжает. Поверх него часовое и
суточное; все три настраиваются (0 выключает окно). Сработавшее окно отдаёт 429 и
Retry-After на свою длину. Redis недоступен — блокируем (fail_closed): оба эндпоинта
security-critical и без входа в аккаунт.
"""

from __future__ import annotations

from fastapi import HTTPException, status

from app.config import settings
from app.utils.cache import RateLimitCache


MINUTE = 60
HOUR = 60 * MINUTE
DAY = 24 * HOUR

#: Перебор recovery-кода: 80 бит не подобрать, но лимит нужен, чтобы не давать грузить БД
#: и чтобы любой промах оставлял след в счётчике. Не настраивается.
RECOVERY_LOGIN_WINDOWS: tuple[tuple[str, int, int], ...] = (
    ('recovery_login', 5, MINUTE),
    ('recovery_login_hour', 20, HOUR),
)


def _signup_windows() -> tuple[tuple[str, int, int], ...]:
    return (
        ('passkey_signup', settings.PASSKEY_SIGNUP_LIMIT_PER_MINUTE, MINUTE),
        ('passkey_signup_hour', settings.PASSKEY_SIGNUP_LIMIT_PER_HOUR, HOUR),
        ('passkey_signup_day', settings.PASSKEY_SIGNUP_LIMIT_PER_DAY, DAY),
    )


async def _enforce(client_ip: str, windows: tuple[tuple[str, int, int], ...]) -> None:
    for action, limit, window in windows:
        if limit <= 0:
            continue
        if await RateLimitCache.is_ip_rate_limited(client_ip, action, limit=limit, window=window, fail_closed=True):
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail='Too many requests',
                headers={'Retry-After': str(window)},
            )


async def enforce_passkey_signup_throttle(client_ip: str) -> None:
    await _enforce(client_ip, _signup_windows())


async def enforce_recovery_login_throttle(client_ip: str) -> None:
    await _enforce(client_ip, RECOVERY_LOGIN_WINDOWS)
