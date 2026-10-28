"""Журнал личностей триала: ключи, нормализация почты, хэш с перцем и атомарное занятие.

Личность аккаунта — набор ключей: ``tg:<telegram_id>``, ``email:<нормализованная почта>``,
``google:<sub>`` (так же ``yandex:``, ``discord:``, ``vk:``). При выдаче триала ключи
записываются в ``trial_identity_ledger``; если хотя бы один уже там, новый триал этой личности
не выдаётся, даже если аккаунт создан заново.

* В таблице только ``HMAC-SHA256(pepper, ключ)``; связи с пользователем нет. Перец берётся
  из ``CABINET_JWT_SECRET``, как у recovery-кодов, — ротация секрета обнуляет память журнала.
* Почта учитывается только подтверждённая: чужой непроверенный адрес нельзя «сжечь».
* У gmail.com/googlemail.com точки и ``+tag`` в локальной части не различают ящики, поэтому
  срезаются; у остальных доменов регистр понижается, остальное сохраняется.
* Занятие ключей — вставка под SAVEPOINT: уникальность хэша решает гонку двух аккаунтов, а
  сессия после отказа остаётся рабочей. Вставка живёт в одной транзакции с подпиской: нет
  подписки (откат) — нет и записи.
* Сброс триала администратором (``trial_reset_at``) открывает триал заново и обходит журнал.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.cabinet.auth.pepper import pepper_secret
from app.database.models import TrialIdentityLedger
from app.services.trial_eligibility import LiteTrialReason, LiteTrialRefused


logger = structlog.get_logger(__name__)

_PEPPER_LABEL = b'bedolaga-trial-identity:'
_GMAIL_DOMAINS = frozenset({'gmail.com', 'googlemail.com'})
#: Поля пользователя, которые идут в ключи как ``<провайдер>:<значение>``.
_PROVIDER_FIELDS: tuple[tuple[str, str], ...] = (
    ('google', 'google_id'),
    ('yandex', 'yandex_id'),
    ('discord', 'discord_id'),
    ('vk', 'vk_id'),
)


def normalize_email(raw: object) -> str | None:
    """Канонический вид адреса для сравнения личностей; ``None``, если адреса нет или он негоден."""
    if not isinstance(raw, str):
        return None
    address = raw.strip().lower()
    local, separator, domain = address.rpartition('@')
    if not separator or not domain:
        return None
    if domain in _GMAIL_DOMAINS:
        local = local.split('+', 1)[0].replace('.', '')
        domain = 'gmail.com'
    if not local:
        return None
    return f'{local}@{domain}'


def identity_keys(user: Any) -> list[str]:
    """Ключи личности аккаунта в стабильном порядке."""
    keys: list[str] = []
    telegram_id = getattr(user, 'telegram_id', None)
    if telegram_id is not None:
        keys.append(f'tg:{telegram_id}')
    if getattr(user, 'email_verified', False):
        email = normalize_email(getattr(user, 'email', None))
        if email:
            keys.append(f'email:{email}')
    for provider, attribute in _PROVIDER_FIELDS:
        value = getattr(user, attribute, None)
        if value not in (None, ''):
            keys.append(f'{provider}:{value}')
    return keys


def _pepper() -> bytes:
    return _PEPPER_LABEL + pepper_secret()


def hash_identity(key: str) -> str:
    """HMAC-SHA256 ключа личности под перцем; hex, 64 символа."""
    return hmac.new(_pepper(), key.encode('utf-8'), hashlib.sha256).hexdigest()


def _hashes_for(user: Any) -> list[str]:
    # Администратор открыл триал заново — журнал на этот раз не участвует.
    if getattr(user, 'trial_reset_at', None) is not None:
        return []
    return [hash_identity(key) for key in identity_keys(user)]


async def ensure_identities_free(db: Any, user: Any) -> None:
    """Бросает ``LiteTrialRefused(IDENTITY_ALREADY_USED)``, если любая личность аккаунта уже в журнале."""
    hashes = _hashes_for(user)
    if not hashes:
        return
    found = await db.execute(
        select(TrialIdentityLedger.id).where(TrialIdentityLedger.identity_hash.in_(hashes)).limit(1)
    )
    if found.first() is not None:
        raise LiteTrialRefused(LiteTrialReason.IDENTITY_ALREADY_USED)


async def claim_identities(db: Any, user: Any) -> list[str]:
    """Записывает ключи личности в журнал; бросает ``LiteTrialRefused``, если любой уже занят.

    Запись делается в текущей транзакции (flush под SAVEPOINT) и закрепляется вместе с подпиской.
    Возвращает записанные хэши.
    """
    hashes = _hashes_for(user)
    if not hashes:
        return []
    try:
        async with db.begin_nested():
            db.add_all([TrialIdentityLedger(identity_hash=value) for value in hashes])
            await db.flush()
    except IntegrityError:
        logger.info('Trial identity already in the ledger, claim refused')
        raise LiteTrialRefused(LiteTrialReason.IDENTITY_ALREADY_USED) from None
    return hashes
