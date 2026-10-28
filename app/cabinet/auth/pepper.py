"""Общий секрет-перец для хэшей, которые нельзя перевыпустить: recovery-коды и журнал личностей триала.

Порядок источников: ``CABINET_PEPPER`` → ``CABINET_JWT_SECRET`` → ``BOT_TOKEN`` (с прежним
предупреждением). Пока ``CABINET_PEPPER`` не задан, результат совпадает с тем, что считалось от
``CABINET_JWT_SECRET``, поэтому уже записанные хэши остаются действительными.

Зачем отдельный перец: ротация ``CABINET_JWT_SECRET`` (подписи сессий) без него обнуляла бы все
recovery-коды и память журнала. Перед ротацией задайте ``CABINET_PEPPER`` текущим значением
``CABINET_JWT_SECRET``. Каждый потребитель добавляет к перцу собственную метку, так что хэши разных
назначений между собой не совпадают.
"""

from __future__ import annotations

from app.config import settings


def pepper_secret() -> bytes:
    """Секрет под перец (без метки назначения)."""
    explicit = settings.CABINET_PEPPER
    if explicit and explicit.strip():
        return explicit.encode()
    return settings.get_cabinet_jwt_secret().encode()
