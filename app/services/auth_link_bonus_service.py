"""Бонус за привязку второго способа входа (docs/auth-link-bonus.md).

Аккаунт, на котором держится платная подписка, получает ``AUTH_LINK_BONUS_DAYS`` к её концу за
ПЕРВУЮ привязку passkey или OAuth-провайдера. Почта и Telegram бонуса не дают. Один раз на аккаунт:
повторная привязка, отвязка с новой привязкой и второй способ его не повторяют.

Гарантии, на которых держится выдача бесплатного платного времени:

* единственная защита от повтора под гонкой — ``UNIQUE(auth_link_bonus.user_id)``. Строка
  вставляется в ТОЙ ЖЕ транзакции, что привязка и продление, а проигравший параллельный запрос
  получает ``IntegrityError`` и ничего не выдаёт;
* вся выдача идёт в savepoint: сбой бонуса откатывает только его (строку и продление), а привязка
  способа входа остаётся в транзакции вызывающего и коммитится им. Бонус не может провалить привязку;
* нечего продлевать (нет активной платной подписки) — не выдаётся ничего и право НЕ тратится: строки
  нет, и следующая привязка, на которой подписка уже есть, бонус выдаст. Подробности — в документе.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.auth_methods import OAUTH_PROVIDER_COLUMNS
from app.database.crud.notification import clear_notifications
from app.database.crud.transaction import REAL_PAYMENT_METHODS
from app.database.models import (
    AuthLinkBonus,
    Subscription,
    SubscriptionStatus,
    Tariff,
    Transaction,
    TransactionType,
    UserPasskey,
)


logger = structlog.get_logger(__name__)

METHOD_PASSKEY = 'passkey'

#: Границы AUTH_LINK_BONUS_DAYS те же, что проверяет админка (NUMERIC_BOUNDS); значение из .env
#: вне границ ограничивается здесь, при чтении.
_FALLBACK_DAYS_BOUNDS = (1, 365)

_PAYMENT_TRANSACTION_TYPES = (TransactionType.DEPOSIT.value, TransactionType.SUBSCRIPTION_PAYMENT.value)


@dataclass(frozen=True, slots=True)
class LinkBonusGrant:
    """Итог выдачи: что и куда добавлено (для уведомления и синхронизации с панелью)."""

    user_id: int
    method: str
    days: int
    subscription_id: int
    new_end_date: datetime


@dataclass(frozen=True, slots=True)
class LinkBonusStatus:
    """Что показывать в кабинете: можно ли получить бонус привязкой и на сколько дней."""

    available: bool
    days: int


def is_enabled() -> bool:
    return bool(settings.AUTH_LINK_BONUS_ENABLED)


def bonus_days() -> int:
    """Дни бонуса, ограниченные теми же границами, что и ввод из админки."""
    from app.services.system_settings_service import BotConfigurationService

    low, high = BotConfigurationService.get_bounds('AUTH_LINK_BONUS_DAYS') or _FALLBACK_DAYS_BOUNDS
    try:
        value = int(settings.AUTH_LINK_BONUS_DAYS)
    except (TypeError, ValueError):
        value = low
    return max(low, min(high, value))


# ---------------------------------------------------------------------------
# Предикаты. Каждый определён здесь один раз: выдача и флаг для баннера читают одно и то же.
# ---------------------------------------------------------------------------


async def has_payment_history(db: AsyncSession, user_id: int) -> bool:
    """Аккаунт платил: есть проведённое пополнение или оплата подписки через платёжный шлюз.

    Мерило то же, что у выручки в админке (``REAL_PAYMENT_METHODS``): начисления администратором,
    оплата с баланса, промокоды, награды и возвраты платежом не считаются, а подписка, выданная без
    оплаты, и триал — тем более.
    """
    found = await db.execute(
        select(Transaction.id)
        .where(
            Transaction.user_id == user_id,
            Transaction.type.in_(_PAYMENT_TRANSACTION_TYPES),
            Transaction.is_completed.is_(True),
            Transaction.payment_method.in_(REAL_PAYMENT_METHODS),
        )
        .limit(1)
    )
    return found.first() is not None


async def has_received_bonus(db: AsyncSession, user_id: int) -> bool:
    found = await db.execute(select(AuthLinkBonus.id).where(AuthLinkBonus.user_id == user_id).limit(1))
    return found.first() is not None


async def has_qualifying_method(db: AsyncSession, user: Any) -> bool:
    """Привязан ли уже passkey или OAuth-провайдер. Telegram и почта не считаются.

    Вызывающий спрашивает ДО того, как применит новую привязку, и передаёт ответ в
    ``grant_link_bonus``: бонус только за первый способ.
    """
    if any(getattr(user, column, None) for column in OAUTH_PROVIDER_COLUMNS.values()):
        return True
    count = await db.execute(select(func.count()).select_from(UserPasskey).where(UserPasskey.user_id == user.id))
    return (count.scalar() or 0) > 0


def _extendable_subscription_query(user_id: int, *, lock: bool):
    """Платная активная подписка, к которой добавляются дни.

    Не триал, статус ``active``, срок не истёк, не суточный тариф (у посуточной подписки дни
    добавлять некуда: плата списывается за каждый день). Из нескольких — с самым поздним концом,
    при равенстве меньший id: порядок полный, дни не «гуляют» между подписками.
    """
    daily_tariffs = select(Tariff.id).where(Tariff.is_daily.is_(True))
    query = (
        select(Subscription)
        .where(
            Subscription.user_id == user_id,
            func.coalesce(Subscription.is_trial, False).is_(False),
            Subscription.status == SubscriptionStatus.ACTIVE.value,
            Subscription.end_date > datetime.now(UTC),
            or_(Subscription.tariff_id.is_(None), Subscription.tariff_id.not_in(daily_tariffs)),
        )
        .order_by(Subscription.end_date.desc(), Subscription.id.asc())
        .limit(1)
        .execution_options(populate_existing=True)
    )
    return query.with_for_update() if lock else query


async def get_link_bonus_status(db: AsyncSession, user: Any) -> LinkBonusStatus:
    """Доступен ли бонус этому аккаунту прямо сейчас (для баннера в кабинете).

    Истина только если выдача при привязке прямо сейчас дала бы дни: функция включена, бонус не
    получен, ни один подходящий способ ещё не привязан, аккаунт платящий (если это требуется) и есть
    активная платная подписка. Без последнего условия баннер обещал бы то, чего привязка не даст.
    """
    if not is_enabled():
        return LinkBonusStatus(available=False, days=0)
    days = bonus_days()
    unavailable = LinkBonusStatus(available=False, days=days)

    if await has_received_bonus(db, user.id) or await has_qualifying_method(db, user):
        return unavailable
    if settings.AUTH_LINK_BONUS_REQUIRES_PAID and not await has_payment_history(db, user.id):
        return unavailable
    if (await db.execute(_extendable_subscription_query(user.id, lock=False))).first() is None:
        return unavailable
    return LinkBonusStatus(available=True, days=days)


# ---------------------------------------------------------------------------
# Выдача
# ---------------------------------------------------------------------------


async def grant_link_bonus(
    db: AsyncSession,
    user: Any,
    method: str,
    *,
    had_qualifying_method: bool,
) -> LinkBonusGrant | None:
    """Выдать бонус за только что применённую привязку. Не бросает; ``None`` — не выдан.

    Вызывается ПОСЛЕ того, как привязка применена в сессии (строка/колонка записаны), и ДО коммита:
    привязка, строка выдачи и продление фиксируются одним коммитом вызывающего. ``had_qualifying_method``
    — результат ``has_qualifying_method`` до привязки. Всё, что делает эта функция, идёт в savepoint:
    при любом сбое откатывается только бонус.
    """
    if not is_enabled() or had_qualifying_method:
        return None

    user_id = user.id  # после отката savepoint обращаться к ORM-объекту нельзя
    method = str(method)[:32]
    days = bonus_days()

    try:
        async with db.begin_nested():
            if await has_received_bonus(db, user_id):
                return None
            if settings.AUTH_LINK_BONUS_REQUIRES_PAID and not await has_payment_history(db, user_id):
                return None

            # Блокировка подписки берётся ДО вставки строки: проигравший гонку ждёт здесь, а затем
            # упирается в UNIQUE и уходит, не успев продлить ничего.
            subscription = (await db.execute(_extendable_subscription_query(user_id, lock=True))).scalars().first()
            if subscription is None:
                return None

            db.add(AuthLinkBonus(user_id=user_id, method=method))
            await db.flush()

            now = datetime.now(UTC)
            subscription.end_date = subscription.end_date + timedelta(days=days)
            subscription.pending_panel_expire_at = subscription.end_date
            subscription.updated_at = now
            # Напоминания «подписка заканчивается» должны сработать заново на новую дату.
            await clear_notifications(db, subscription.id, commit=False)
            await db.flush()

            grant = LinkBonusGrant(
                user_id=user_id,
                method=method,
                days=days,
                subscription_id=subscription.id,
                new_end_date=subscription.end_date,
            )
    except IntegrityError:
        logger.info('Link bonus refused: already granted by a concurrent request', user_id=user_id, method=method)
        return None
    except Exception:
        logger.exception('Link bonus failed, the link itself is kept', user_id=user_id, method=method)
        return None

    logger.info(
        'Link bonus granted',
        user_id=user_id,
        method=method,
        days=grant.days,
        subscription_id=grant.subscription_id,
    )
    return grant
