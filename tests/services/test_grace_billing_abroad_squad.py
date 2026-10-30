"""Оплата в grace у клиента в режиме «за границей» не должна сбрасывать сквад Abroad."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.config import settings
from app.services.grace_access_runtime import _subscription_to_billing


ABROAD = '0fbd83c2-7c5d-4267-91d3-aaa9b6415a92'
TARIFF_SQUAD = 'a5bb5317-27ca-444a-b030-94daddcc94c7'


def _subscription(*, abroad_mode: bool, tariff_squad: str | None):
    tariff = SimpleNamespace(external_squad_uuid=tariff_squad, is_daily=False, is_free=False)
    return SimpleNamespace(
        id=1,
        remnawave_id=77,
        user=SimpleNamespace(remnawave_id=77, status='active'),
        tariff=tariff,
        traffic_limit_gb=0,
        traffic_used_gb=0.0,
        actual_status='expired',
        end_date=datetime.now(UTC) - timedelta(hours=1),
        device_limit=3,
        connected_squads=['04dfc124-b2dc-4b30-8dd3-bc93ff5e7e20'],
        is_trial=False,
        status='expired',
        grace_suppressed_until=None,
        abroad_mode=abroad_mode,
    )


@pytest.fixture
def abroad_enabled(monkeypatch):
    monkeypatch.setattr(settings, 'ABROAD_MODE_ENABLED', True)
    monkeypatch.setattr(settings, 'ABROAD_EXTERNAL_SQUAD_UUID', ABROAD)


def test_abroad_subscription_keeps_abroad_squad_in_billing_state(abroad_enabled):
    billing = _subscription_to_billing(_subscription(abroad_mode=True, tariff_squad=TARIFF_SQUAD))
    assert billing.external_squad_uuid == ABROAD


def test_abroad_subscription_without_tariff_squad_keeps_abroad(abroad_enabled):
    billing = _subscription_to_billing(_subscription(abroad_mode=True, tariff_squad=None))
    assert billing.external_squad_uuid == ABROAD


def test_home_subscription_uses_tariff_squad(abroad_enabled):
    billing = _subscription_to_billing(_subscription(abroad_mode=False, tariff_squad=TARIFF_SQUAD))
    assert billing.external_squad_uuid == TARIFF_SQUAD


def test_abroad_flag_is_ignored_when_feature_is_off(monkeypatch):
    monkeypatch.setattr(settings, 'ABROAD_MODE_ENABLED', False)
    billing = _subscription_to_billing(_subscription(abroad_mode=True, tariff_squad=TARIFF_SQUAD))
    assert billing.external_squad_uuid == TARIFF_SQUAD
