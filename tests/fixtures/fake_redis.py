"""Redis в памяти с управляемыми часами — ровно столько, сколько зовёт ``app.utils.cache``.

``cache.set/getdel`` и ``RateLimitCache`` ходят в ``cache.redis_client`` настоящим
кодом приложения; здесь подменяется только сервер. Время двигает тест
(``advance``), поэтому TTL челленджа и окно rate-limit проверяются без sleep.
"""

from __future__ import annotations

import hashlib


class FakeRedis:
    def __init__(self) -> None:
        self.now = 1_000_000.0
        self._data: dict[str, tuple[str | bytes, float | None]] = {}
        self._scripts: dict[str, str] = {}

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def _purge(self) -> None:
        for key in [k for k, (_, exp) in self._data.items() if exp is not None and exp <= self.now]:
            del self._data[key]

    def ttl(self, key: str) -> int | None:
        """Оставшееся время жизни ключа в секундах (None — ключа нет или нет TTL)."""
        self._purge()
        entry = self._data.get(key)
        if entry is None or entry[1] is None:
            return None
        return int(entry[1] - self.now)

    def keys_with_prefix(self, prefix: str) -> list[str]:
        self._purge()
        return [k for k in self._data if k.startswith(prefix)]

    async def ping(self) -> bool:
        return True

    async def close(self) -> None:
        return None

    async def set(self, key, value, ex=None, nx=False):
        self._purge()
        if nx and key in self._data:
            return None
        self._data[key] = (value, self.now + ex if ex else None)
        return True

    async def get(self, key):
        self._purge()
        entry = self._data.get(key)
        return entry[0] if entry else None

    async def getdel(self, key):
        self._purge()
        entry = self._data.pop(key, None)
        return entry[0] if entry else None

    async def delete(self, *keys):
        self._purge()
        return sum(1 for key in keys if self._data.pop(key, None) is not None)

    async def script_load(self, script: str) -> str:
        sha = hashlib.sha1(script.encode()).hexdigest()
        self._scripts[sha] = script
        return sha

    async def evalsha(self, sha: str, numkeys: int, key: str, window):
        """INCR + EXPIRE при создании — единственный скрипт RateLimitCache."""
        self._purge()
        current, expires = self._data.get(key, (0, None))
        current = int(current) + 1
        if current == 1:
            expires = self.now + int(window)
        self._data[key] = (str(current), expires)
        return current
