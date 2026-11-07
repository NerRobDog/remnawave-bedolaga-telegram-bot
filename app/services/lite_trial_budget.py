"""Бюджет лёгких триалов в Redis: общий дневной лимит и лимит на IP.

* Сутки — календарный день Europe/Moscow: счётчики лежат под ключом с датой и живут до конца
  суток с небольшим запасом, так что «утром обнуляется» не зависит от работающего процесса.
* Резерв атомарный: проверка лимитов и увеличение счётчиков — один Lua-скрипт, гонки
  «проверил, потом записал» нет. Отказ по одному лимиту не тратит другой.
* Резерв делается ДО активации и возвращается (``release``), если активация не удалась.
* Redis недоступен или ответил ошибкой — триал не выдаётся (fail-closed): без счётчика потолок
  расходов гарантировать нечем.
* IPv6 считается по сети /64: один пользователь легко меняет адрес внутри неё.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import structlog

from app.services.trial_eligibility import LiteTrialReason, LiteTrialRefused, get_lite_trial_limits
from app.utils.cache import cache


logger = structlog.get_logger(__name__)

BUDGET_TIMEZONE = ZoneInfo('Europe/Moscow')
_KEY_PREFIX = 'lite_trial'
#: Запас сверх конца суток: счётчик не исчезает ровно в полночь посреди чьей-то активации.
_TTL_MARGIN_SECONDS = 3600

# Скрипты выполняются в Redis целиком. Первая строка-комментарий — метка для тестового двойника.
_RESERVE_SCRIPT = """
-- lite_trial:reserve
local daily_limit = tonumber(ARGV[1])
local ip_limit = tonumber(ARGV[2])
local ttl = tonumber(ARGV[3])
local daily = tonumber(redis.call('GET', KEYS[1]) or '0')
if daily >= daily_limit then
    return 1
end
if ip_limit > 0 then
    local per_ip = tonumber(redis.call('GET', KEYS[2]) or '0')
    if per_ip >= ip_limit then
        return 2
    end
    redis.call('INCR', KEYS[2])
    redis.call('EXPIRE', KEYS[2], ttl)
end
redis.call('INCR', KEYS[1])
redis.call('EXPIRE', KEYS[1], ttl)
return 0
"""

_RELEASE_SCRIPT = """
-- lite_trial:release
for _, key in ipairs(KEYS) do
    local count = tonumber(redis.call('GET', key) or '0')
    if count > 0 then
        redis.call('DECR', key)
    end
end
return 0
"""

_REFUSAL_BY_CODE = {
    1: LiteTrialReason.DAILY_LIMIT_REACHED,
    2: LiteTrialReason.IP_LIMIT_REACHED,
}


def _now() -> datetime:
    return datetime.now(UTC)


def _today() -> tuple[str, int]:
    """Московская дата (для ключей) и TTL в секундах до конца этих суток с запасом."""
    local = _now().astimezone(BUDGET_TIMEZONE)
    next_midnight = datetime.combine(local.date() + timedelta(days=1), datetime.min.time(), tzinfo=BUDGET_TIMEZONE)
    return local.date().isoformat(), int((next_midnight - local).total_seconds()) + _TTL_MARGIN_SECONDS


def _ip_bucket(ip: str) -> str:
    try:
        address = ipaddress.ip_address(ip.strip())
    except ValueError:
        return ip.strip().lower()
    if address.version == 6:
        return str(ipaddress.ip_network(f'{address}/64', strict=False))
    return str(address)


def _daily_key(day: str) -> str:
    return f'{_KEY_PREFIX}:{day}:daily'


def _ip_key(day: str, ip: str) -> str:
    return f'{_KEY_PREFIX}:{day}:ip:{_ip_bucket(ip)}'


def _redis():
    """Живой клиент Redis или отказ: без него бюджет не посчитать."""
    if not cache._connected or cache.redis_client is None:
        logger.warning('Lite trial budget unavailable: Redis disconnected')
        raise LiteTrialRefused(LiteTrialReason.NOT_AVAILABLE)
    return cache.redis_client


@dataclass
class LiteTrialReservation:
    """Занятые слоты бюджета; ``release`` возвращает их, если триал выдать не удалось."""

    keys: tuple[str, ...]
    released: bool = field(default=False)

    async def release(self) -> None:
        if self.released:
            return
        self.released = True
        try:
            await _redis().eval(_RELEASE_SCRIPT, len(self.keys), *self.keys)
        except Exception:
            # Слот не вернулся: бюджет остаётся заниженным до конца суток, это безопасная сторона.
            logger.warning('Failed to release lite trial reservation', keys=self.keys, exc_info=True)


async def reserve_lite_trial(client_ip: str | None) -> LiteTrialReservation:
    """Занимает слот под лёгкий триал или бросает ``LiteTrialRefused`` с причиной."""
    limits = get_lite_trial_limits()
    if limits.daily_limit <= 0:
        raise LiteTrialRefused(LiteTrialReason.NOT_AVAILABLE)
    use_ip = limits.per_ip_daily_limit > 0
    if use_ip and not client_ip:
        raise LiteTrialRefused(LiteTrialReason.NOT_AVAILABLE)

    redis = _redis()
    day, ttl = _today()
    keys = [_daily_key(day)]
    if use_ip:
        keys.append(_ip_key(day, client_ip))
    # KEYS[2] нужен скрипту всегда: при выключенном лимите на IP он просто не читается.
    script_keys = [keys[0], keys[1] if use_ip else keys[0]]
    try:
        verdict = int(
            await redis.eval(
                _RESERVE_SCRIPT,
                len(script_keys),
                *script_keys,
                limits.daily_limit,
                limits.per_ip_daily_limit,
                ttl,
            )
        )
    except Exception:
        logger.warning('Lite trial reservation failed: Redis error', exc_info=True)
        raise LiteTrialRefused(LiteTrialReason.NOT_AVAILABLE) from None

    if verdict != 0:
        raise LiteTrialRefused(_REFUSAL_BY_CODE.get(verdict, LiteTrialReason.NOT_AVAILABLE))
    return LiteTrialReservation(keys=tuple(keys))


async def peek_lite_trial(client_ip: str | None) -> LiteTrialReason | None:
    """Причина, по которой лёгкий триал сейчас не выдать, или ``None``. Счётчики не меняет."""
    limits = get_lite_trial_limits()
    if limits.daily_limit <= 0:
        return LiteTrialReason.NOT_AVAILABLE
    use_ip = limits.per_ip_daily_limit > 0
    if use_ip and not client_ip:
        return LiteTrialReason.NOT_AVAILABLE
    try:
        redis = _redis()
        day, _ = _today()
        daily = int(await redis.get(_daily_key(day)) or 0)
        if daily >= limits.daily_limit:
            return LiteTrialReason.DAILY_LIMIT_REACHED
        if use_ip and int(await redis.get(_ip_key(day, client_ip)) or 0) >= limits.per_ip_daily_limit:
            return LiteTrialReason.IP_LIMIT_REACHED
    except LiteTrialRefused as refused:
        return refused.reason
    except Exception:
        logger.warning('Lite trial budget peek failed: Redis error', exc_info=True)
        return LiteTrialReason.NOT_AVAILABLE
    return None


async def used_today() -> int:
    """Сколько лёгких триалов занято в бюджете за сегодняшние (московские) сутки."""
    day, _ = _today()
    return int(await _redis().get(_daily_key(day)) or 0)
