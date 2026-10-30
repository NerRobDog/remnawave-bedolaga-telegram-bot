"""Динамические кнопки быстрых сумм пополнения.

Кнопки считаются под пользователя: цена продления его текущей подписки за 1, 3, 6 … месяцев
(со всеми его скидками) и, если включено, за вычетом уже лежащего на балансе. Так человек
пополняет ровно столько, сколько не хватает на продление.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

from app.config import settings
from app.services.renewal_options_service import list_renewal_options


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.database.models import Subscription, User


logger = structlog.get_logger(__name__)


async def resolve_subscription(db: AsyncSession, user: User, subscription_id: int | None) -> Subscription | None:
    # Выбор подписки общий с кабинетом (в т.ч. мульти-тариф). Импорт отложен: пакет
    # app.cabinet.routes тянет balance.py, а тот — этот модуль.
    from app.cabinet.routes.subscription_modules.helpers import resolve_subscription as _resolve

    return await _resolve(db, user, subscription_id)


MAX_DYNAMIC_PRESETS = 4
_KOPEKS_IN_RUBLE = 100


def build_topup_presets(
    prices_kopeks: Sequence[int],
    *,
    balance_kopeks: int,
    deduct_balance: bool,
    min_kopeks: int,
    max_kopeks: int,
    limit: int = MAX_DYNAMIC_PRESETS,
) -> list[int]:
    """Суммы кнопок в копейках: по возрастанию, без повторов, не больше ``limit``.

    - при ``deduct_balance`` из цены вычитается баланс (отрицательный баланс считается нулём:
      долг не увеличивает сумму кнопки, платёж остаётся ценой продления);
    - сумма округляется вверх до целого рубля;
    - сумма зажимается в [min_kopeks, max_kopeks] метода; если границы перевёрнуты — кнопок нет;
    - цены и остатки <= 0 (период уже покрыт балансом или скидка 100 %) кнопки не дают.
    """
    if max_kopeks < min_kopeks:
        return []

    credit = max(0, balance_kopeks) if deduct_balance else 0
    amounts: set[int] = set()
    for price in prices_kopeks:
        shortfall = price - credit
        if shortfall <= 0:
            continue
        rounded = math.ceil(shortfall / _KOPEKS_IN_RUBLE) * _KOPEKS_IN_RUBLE
        amounts.add(min(max(rounded, min_kopeks), max_kopeks))

    return sorted(amounts)[:limit]


@dataclass(frozen=True)
class TopupPresetsBasis:
    """Исходные данные пользователя; границы метода подставляются при расчёте под каждый метод."""

    prices_kopeks: tuple[int, ...] | list[int]
    balance_kopeks: int
    deduct_balance: bool

    def amounts_for(self, min_kopeks: int, max_kopeks: int) -> list[int]:
        return build_topup_presets(
            self.prices_kopeks,
            balance_kopeks=self.balance_kopeks,
            deduct_balance=self.deduct_balance,
            min_kopeks=min_kopeks,
            max_kopeks=max_kopeks,
        )


async def load_topup_presets_basis(db: AsyncSession, user: User) -> TopupPresetsBasis | None:
    """Цены продления пользователя или ``None``, если динамических кнопок не будет.

    ``None`` при выключенной функции, без подписки, у суточных тарифов без ``period_prices``
    и при любой ошибке расчёта: вызывающий код тогда показывает обычные статические суммы.
    """
    if not settings.TOPUP_DYNAMIC_PRESETS_ENABLED:
        return None
    try:
        subscription = await resolve_subscription(db, user, None)
        options = await list_renewal_options(db, user, subscription)
    except Exception as error:
        logger.warning(
            'Не удалось посчитать динамические суммы пополнения', user_id=user.id, error=error, exc_info=True
        )
        return None

    prices = [option.final_total for option in options]
    if not prices:
        return None
    return TopupPresetsBasis(
        prices_kopeks=prices,
        balance_kopeks=int(user.balance_kopeks or 0),
        deduct_balance=bool(settings.TOPUP_PRESETS_DEDUCT_BALANCE),
    )
