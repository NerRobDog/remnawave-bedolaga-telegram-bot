"""Варианты продления подписки с ценами — единый расчёт для кабинета и быстрых сумм пополнения."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.config import settings
from app.database.models import SubscriptionStatus
from app.services.pricing_engine import pricing_engine


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.database.models import Subscription, User


@dataclass(frozen=True)
class RenewalOption:
    period_days: int
    final_total: int  # kopeks — столько спишется при продлении (с учётом всех скидок)
    original_total: int  # kopeks — цена до скидок
    discount_percent: int
    is_highlighted: bool


async def list_renewal_options(db: AsyncSession, user: User, subscription: Subscription | None) -> list[RenewalOption]:
    """Периоды продления подписки и их цена для конкретного пользователя.

    Цена приходит из ``pricing_engine.calculate_renewal_price`` — того же расчёта, по которому
    кабинет и бот списывают деньги при продлении, поэтому скидки (промогруппа, промо-предложение)
    здесь не дублируются.
    """
    if not subscription:
        return []

    # Classic subscriptions cannot be renewed when tariff mode is enabled
    if settings.is_tariffs_mode() and not subscription.tariff_id:
        return []

    _non_renewable = {SubscriptionStatus.DISABLED.value, SubscriptionStatus.PENDING.value}
    _actual_status = getattr(subscription, 'actual_status', subscription.status)
    if _actual_status in _non_renewable:
        return []

    # Скрытый/неактивный тариф (например, триальный после промокода) —
    # не показываем его периоды, используем стандартные
    if (
        subscription.tariff_id
        and subscription.tariff
        and subscription.tariff.is_active
        and subscription.tariff.period_prices
    ):
        periods = sorted(int(k) for k in subscription.tariff.period_prices.keys())
        highlighted_period = subscription.tariff.highlight_period_days
    else:
        periods = settings.get_available_renewal_periods()
        highlighted_period = None

    # Нулевая цена — не всегда поломка: у бесплатного тарифа она настроена
    # намеренно. Пропускаем период, только если цена не проставлена вовсе.
    renewal_tariff = subscription.tariff if subscription.tariff_id else None

    options: list[RenewalOption] = []
    for period in periods:
        pricing = await pricing_engine.calculate_renewal_price(db, subscription, period, user=user)

        if pricing.final_total <= 0 and pricing.original_total <= 0:
            if renewal_tariff is None or not renewal_tariff.has_configured_price_for_period(period):
                continue

        original_price = pricing.original_total
        combined_discount = 0
        if original_price > 0 and original_price != pricing.final_total:
            combined_discount = int((original_price - pricing.final_total) * 100 / original_price)

        options.append(
            RenewalOption(
                period_days=period,
                final_total=pricing.final_total,
                original_total=original_price,
                discount_percent=combined_discount,
                # Выделение живёт у тарифа. Когда периоды берутся не из тарифа
                # (скрытый тариф, классический режим), выделять нечего.
                is_highlighted=bool(highlighted_period is not None and highlighted_period == period),
            )
        )

    return options
