"""Динамические кнопки быстрых сумм пополнения: цена продления пользователя минус баланс."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import settings
from app.database.models import PromoGroup, Subscription, SubscriptionStatus, Tariff, User
from app.services import payment_method_config_service as method_config, topup_presets
from app.services.topup_presets import build_topup_presets


MIN_KOPEKS = 10000  # 100 ₽
MAX_KOPEKS = 10_000_000  # 100 000 ₽


class TestBuildTopupPresets:
    def test_deduct_balance_subtracts_from_each_price(self):
        result = build_topup_presets(
            [40000, 100000, 180000],
            balance_kopeks=15000,
            deduct_balance=True,
            min_kopeks=MIN_KOPEKS,
            max_kopeks=MAX_KOPEKS,
        )
        assert result == [25000, 85000, 165000]

    def test_without_deduction_shows_full_prices(self):
        result = build_topup_presets(
            [40000, 100000, 180000],
            balance_kopeks=15000,
            deduct_balance=False,
            min_kopeks=MIN_KOPEKS,
            max_kopeks=MAX_KOPEKS,
        )
        assert result == [40000, 100000, 180000]

    def test_balance_covering_a_period_drops_its_button(self):
        result = build_topup_presets(
            [40000, 100000, 180000],
            balance_kopeks=40000,
            deduct_balance=True,
            min_kopeks=MIN_KOPEKS,
            max_kopeks=MAX_KOPEKS,
        )
        assert result == [60000, 140000]

    def test_balance_covering_everything_gives_nothing(self):
        result = build_topup_presets(
            [40000, 100000], balance_kopeks=500000, deduct_balance=True, min_kopeks=MIN_KOPEKS, max_kopeks=MAX_KOPEKS
        )
        assert result == []

    def test_rounds_up_to_whole_rubles(self):
        result = build_topup_presets(
            [40001, 100050], balance_kopeks=0, deduct_balance=True, min_kopeks=MIN_KOPEKS, max_kopeks=MAX_KOPEKS
        )
        assert result == [40100, 100100]

    def test_rounds_up_the_shortfall_not_the_price(self):
        # 400.50 ₽ минус 150.25 ₽ = 250.25 ₽ → 251 ₽
        result = build_topup_presets(
            [40050], balance_kopeks=15025, deduct_balance=True, min_kopeks=MIN_KOPEKS, max_kopeks=MAX_KOPEKS
        )
        assert result == [25100]

    def test_small_shortfall_is_raised_to_method_minimum(self):
        result = build_topup_presets(
            [40000], balance_kopeks=39900, deduct_balance=True, min_kopeks=MIN_KOPEKS, max_kopeks=MAX_KOPEKS
        )
        assert result == [MIN_KOPEKS]

    def test_large_amount_is_capped_at_method_maximum(self):
        result = build_topup_presets(
            [300000, 600000], balance_kopeks=0, deduct_balance=True, min_kopeks=MIN_KOPEKS, max_kopeks=500000
        )
        assert result == [300000, 500000]

    def test_duplicates_collapse(self):
        result = build_topup_presets(
            [40000, 40000, 100000, 100000],
            balance_kopeks=0,
            deduct_balance=True,
            min_kopeks=MIN_KOPEKS,
            max_kopeks=MAX_KOPEKS,
        )
        assert result == [40000, 100000]

    def test_clamping_can_create_duplicates_that_collapse(self):
        result = build_topup_presets(
            [20000, 30000], balance_kopeks=15000, deduct_balance=True, min_kopeks=MIN_KOPEKS, max_kopeks=MAX_KOPEKS
        )
        # 50 ₽ и 150 ₽ → 100 ₽ (минимум) и 150 ₽
        assert result == [MIN_KOPEKS, 15000]

    def test_free_prices_are_dropped(self):
        # скидка 100 % → цена 0: кнопки «пополнить на 0 ₽» не бывает
        result = build_topup_presets(
            [0, 0, 0], balance_kopeks=0, deduct_balance=False, min_kopeks=MIN_KOPEKS, max_kopeks=MAX_KOPEKS
        )
        assert result == []

    def test_negative_prices_are_dropped(self):
        result = build_topup_presets(
            [-100, 40000], balance_kopeks=0, deduct_balance=False, min_kopeks=MIN_KOPEKS, max_kopeks=MAX_KOPEKS
        )
        assert result == [40000]

    def test_negative_balance_is_not_added_to_the_price(self):
        # долг на балансе: платёж на цену продления, а не больше — иначе кнопка обманывает
        result = build_topup_presets(
            [40000], balance_kopeks=-5000, deduct_balance=True, min_kopeks=MIN_KOPEKS, max_kopeks=MAX_KOPEKS
        )
        assert result == [40000]

    def test_at_most_four_ascending_buttons(self):
        result = build_topup_presets(
            [500000, 100000, 300000, 200000, 400000, 600000],
            balance_kopeks=0,
            deduct_balance=True,
            min_kopeks=MIN_KOPEKS,
            max_kopeks=MAX_KOPEKS,
        )
        assert result == [100000, 200000, 300000, 400000]

    def test_inverted_limits_give_nothing(self):
        assert (
            build_topup_presets([40000], balance_kopeks=0, deduct_balance=True, min_kopeks=500000, max_kopeks=100000)
            == []
        )


def _tariff(period_prices: dict[str, int] | None = None, **kwargs) -> Tariff:
    return Tariff(
        id=kwargs.pop('id', 2),
        name='T',
        is_active=True,
        is_daily=kwargs.pop('is_daily', False),
        daily_price_kopeks=kwargs.pop('daily_price_kopeks', 0),
        period_prices=period_prices if period_prices is not None else {},
        device_limit=3,
        device_price_kopeks=0,
        **kwargs,
    )


def _user(balance: int, **kwargs) -> User:
    return User(id=7, telegram_id=1, balance_kopeks=balance, **kwargs)


def _sub(tariff: Tariff | None, status: str = SubscriptionStatus.ACTIVE.value) -> Subscription:
    sub = Subscription(id=11, user_id=7, status=status, device_limit=3, tariff_id=tariff.id if tariff else None)
    sub.tariff = tariff
    return sub


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(settings, 'TOPUP_DYNAMIC_PRESETS_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'TOPUP_PRESETS_DEDUCT_BALANCE', True, raising=False)
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', False, raising=False)
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs', raising=False)


def _patch_subscription(monkeypatch, subscription):
    monkeypatch.setattr(topup_presets, 'resolve_subscription', AsyncMock(return_value=subscription))


class TestLoadTopupBasis:
    async def test_tariff_prices_follow_the_users_renewal_price(self, monkeypatch, enabled):
        tariff = _tariff({'30': 40000, '90': 100000, '180': 180000, '360': 300000})
        _patch_subscription(monkeypatch, _sub(tariff))

        basis = await topup_presets.load_topup_presets_basis(MagicMock(), _user(15000))

        assert basis is not None
        assert basis.amounts_for(MIN_KOPEKS, MAX_KOPEKS) == [25000, 85000, 165000, 285000]

    async def test_promo_offer_discount_is_applied_via_pricing_engine(self, monkeypatch, enabled):
        tariff = _tariff({'30': 50000, '90': 150000})
        _patch_subscription(monkeypatch, _sub(tariff))
        user = _user(0, promo_offer_discount_percent=20)

        basis = await topup_presets.load_topup_presets_basis(MagicMock(), user)

        assert basis is not None
        assert basis.amounts_for(MIN_KOPEKS, MAX_KOPEKS) == [40000, 120000]

    async def test_promo_group_discount_is_applied(self, monkeypatch, enabled):
        tariff = _tariff({'30': 50000, '90': 150000})
        _patch_subscription(monkeypatch, _sub(tariff))
        group = PromoGroup(id=3, name='vip', priority=1, period_discounts={'30': 10, '90': 10})
        user = _user(0)
        user.promo_group = group

        basis = await topup_presets.load_topup_presets_basis(MagicMock(), user)

        assert basis is not None
        assert basis.amounts_for(MIN_KOPEKS, MAX_KOPEKS) == [45000, 135000]

    async def test_full_discount_leaves_no_buttons(self, monkeypatch, enabled):
        tariff = _tariff({'30': 50000, '90': 150000})
        _patch_subscription(monkeypatch, _sub(tariff))

        basis = await topup_presets.load_topup_presets_basis(MagicMock(), _user(0, promo_offer_discount_percent=100))

        assert basis is not None
        assert basis.amounts_for(MIN_KOPEKS, MAX_KOPEKS) == []

    async def test_toggle_off_shows_full_prices(self, monkeypatch, enabled):
        monkeypatch.setattr(settings, 'TOPUP_PRESETS_DEDUCT_BALANCE', False, raising=False)
        tariff = _tariff({'30': 40000, '90': 100000})
        _patch_subscription(monkeypatch, _sub(tariff))

        basis = await topup_presets.load_topup_presets_basis(MagicMock(), _user(15000))

        assert basis is not None
        assert basis.amounts_for(MIN_KOPEKS, MAX_KOPEKS) == [40000, 100000]

    async def test_feature_disabled_returns_none(self, monkeypatch, enabled):
        monkeypatch.setattr(settings, 'TOPUP_DYNAMIC_PRESETS_ENABLED', False, raising=False)
        resolve = AsyncMock()
        monkeypatch.setattr(topup_presets, 'resolve_subscription', resolve)

        assert await topup_presets.load_topup_presets_basis(MagicMock(), _user(0)) is None
        resolve.assert_not_awaited()

    async def test_no_subscription_returns_none(self, monkeypatch, enabled):
        _patch_subscription(monkeypatch, None)

        assert await topup_presets.load_topup_presets_basis(MagicMock(), _user(0)) is None

    async def test_daily_tariff_without_period_prices_returns_none(self, monkeypatch, enabled):
        tariff = _tariff({}, is_daily=True, daily_price_kopeks=2000)
        _patch_subscription(monkeypatch, _sub(tariff))

        assert await topup_presets.load_topup_presets_basis(MagicMock(), _user(0)) is None

    async def test_disabled_subscription_returns_none(self, monkeypatch, enabled):
        tariff = _tariff({'30': 40000})
        _patch_subscription(monkeypatch, _sub(tariff, status=SubscriptionStatus.DISABLED.value))

        assert await topup_presets.load_topup_presets_basis(MagicMock(), _user(0)) is None

    async def test_calculation_error_returns_none(self, monkeypatch, enabled):
        monkeypatch.setattr(topup_presets, 'resolve_subscription', AsyncMock(side_effect=RuntimeError('boom')))

        assert await topup_presets.load_topup_presets_basis(MagicMock(), _user(0)) is None


def _config(quick_amounts):
    return MagicMock(
        method_id='sample_method',
        is_enabled=True,
        user_type_filter='all',
        first_topup_filter='any',
        promo_group_filter_mode='all',
        allowed_promo_groups=[],
        display_name='Sample method',
        description=None,
        min_amount_kopeks=MIN_KOPEKS,
        max_amount_kopeks=MAX_KOPEKS,
        sub_options=None,
        quick_amounts=quick_amounts,
        sort_order=1,
        open_url_direct=False,
    )


class TestMethodsUseDynamicPresets:
    @pytest.fixture(autouse=True)
    def _wire(self, monkeypatch):
        monkeypatch.setattr(
            method_config,
            '_get_method_defaults',
            lambda: {'sample_method': {'is_configured': True, 'default_min': MIN_KOPEKS, 'default_max': MAX_KOPEKS}},
        )

    async def _methods(self, monkeypatch, quick_amounts, basis):
        monkeypatch.setattr(method_config, 'get_all_configs', AsyncMock(return_value=[_config(quick_amounts)]))
        return await method_config.get_enabled_methods_for_user(MagicMock(), user=_user(0), topup_presets=basis)

    async def test_null_quick_amounts_use_dynamic_presets(self, monkeypatch):
        basis = topup_presets.TopupPresetsBasis(
            prices_kopeks=[40000, 100000], balance_kopeks=15000, deduct_balance=True
        )

        (method,) = await self._methods(monkeypatch, None, basis)

        assert method['quick_amounts'] == [25000, 85000]

    async def test_explicit_admin_list_wins(self, monkeypatch):
        basis = topup_presets.TopupPresetsBasis(prices_kopeks=[40000], balance_kopeks=0, deduct_balance=True)

        (method,) = await self._methods(monkeypatch, [20000, 30000], basis)

        assert method['quick_amounts'] == [20000, 30000]

    async def test_explicitly_empty_list_stays_empty(self, monkeypatch):
        basis = topup_presets.TopupPresetsBasis(prices_kopeks=[40000], balance_kopeks=0, deduct_balance=True)

        (method,) = await self._methods(monkeypatch, [], basis)

        assert method['quick_amounts'] == []

    async def test_no_basis_falls_back_to_static_defaults(self, monkeypatch):
        (method,) = await self._methods(monkeypatch, None, None)

        assert method['quick_amounts'] == [10000, 30000, 50000, 100000]

    async def test_empty_dynamic_result_falls_back_to_static_defaults(self, monkeypatch):
        basis = topup_presets.TopupPresetsBasis(prices_kopeks=[40000], balance_kopeks=900000, deduct_balance=True)

        (method,) = await self._methods(monkeypatch, None, basis)

        assert method['quick_amounts'] == [10000, 30000, 50000, 100000]
