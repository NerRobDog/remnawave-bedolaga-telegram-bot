"""Переключатель «за границей»: домашний сквад не теряется, чужие правки не затираются."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.external.remnawave_errors import RemnaWaveAPIError
from app.services import abroad_mode_service as svc


ABROAD = '0fbd83c2-7c5d-4267-91d3-aaa9b6415a92'
TEST1 = '11111111-1111-1111-1111-111111111111'
TARIFF_SQUAD = '22222222-2222-2222-2222-222222222222'


class FakePanel:
    """Панель: хранит сквад пользователя; ``stale`` имитирует тихий повтор без поля."""

    def __init__(self, squad=None, *, fail=False, stale_for=()):
        self.squad = squad
        self.fail = fail
        self.stale_for = set(stale_for)
        self.writes = []

    async def get_user_by_id(self, user_id):
        return SimpleNamespace(external_squad_uuid=self.squad)

    async def update_user(self, user_id, external_squad_uuid):
        if self.fail:
            raise RemnaWaveAPIError('boom', 500)
        self.writes.append(external_squad_uuid)
        if external_squad_uuid not in self.stale_for:
            self.squad = external_squad_uuid
        return SimpleNamespace(external_squad_uuid=self.squad)


def _sub(*, mode=False, home=None, tariff_squad=None):
    tariff = SimpleNamespace(external_squad_uuid=tariff_squad) if tariff_squad else None
    return SimpleNamespace(id=1, remnawave_id=144, abroad_mode=mode, abroad_home_squad_uuid=home, tariff=tariff)


@pytest.fixture(autouse=True)
def _enabled():
    with (
        patch.object(svc.settings, 'ABROAD_MODE_ENABLED', True),
        patch.object(svc.settings, 'ABROAD_EXTERNAL_SQUAD_UUID', ABROAD),
    ):
        yield


def _db():
    return SimpleNamespace(commit=AsyncMock())


@pytest.mark.parametrize('home', [None, TEST1])
async def test_round_trip_restores_home_squad(home):
    panel, sub = FakePanel(home), _sub()
    await svc.set_abroad_mode(_db(), panel, sub, True)
    assert panel.squad == ABROAD and sub.abroad_mode and sub.abroad_home_squad_uuid == home
    state = await svc.set_abroad_mode(_db(), panel, sub, False)
    assert panel.squad == home and not sub.abroad_mode and sub.abroad_home_squad_uuid is None
    assert state.abroad is False


async def test_exit_prefers_current_tariff_squad():
    panel, sub = FakePanel(ABROAD), _sub(mode=True, home=TEST1, tariff_squad=TARIFF_SQUAD)
    await svc.set_abroad_mode(_db(), panel, sub, False)
    assert panel.squad == TARIFF_SQUAD


async def test_enable_when_already_on_abroad_keeps_saved_home():
    panel, sub = FakePanel(ABROAD), _sub(mode=True, home=TEST1)
    state = await svc.set_abroad_mode(_db(), panel, sub, True)
    assert not state.changed and sub.abroad_home_squad_uuid == TEST1 and panel.writes == []


async def test_exit_does_not_overwrite_squad_changed_by_admin():
    panel, sub = FakePanel(TEST1), _sub(mode=True, home=None)
    await svc.set_abroad_mode(_db(), panel, sub, False)
    assert panel.writes == [] and panel.squad == TEST1 and not sub.abroad_mode


async def test_panel_failure_rolls_back_local_state():
    panel, sub = FakePanel(TEST1, fail=True), _sub()
    with pytest.raises(svc.AbroadModePanelError):
        await svc.set_abroad_mode(_db(), panel, sub, True)
    assert not sub.abroad_mode and sub.abroad_home_squad_uuid is None


async def test_silent_stale_retry_counts_as_failure():
    panel, sub = FakePanel(TEST1, stale_for={ABROAD}), _sub()
    with pytest.raises(svc.AbroadModePanelError):
        await svc.set_abroad_mode(_db(), panel, sub, True)
    assert not sub.abroad_mode


async def test_deleted_home_squad_falls_back_to_detach():
    panel, sub = FakePanel(ABROAD, stale_for={TEST1}), _sub(mode=True, home=TEST1)
    await svc.set_abroad_mode(_db(), panel, sub, False)
    assert panel.squad is None and not sub.abroad_mode


async def test_disabled_flag_blocks_switch():
    with patch.object(svc.settings, 'ABROAD_MODE_ENABLED', False):
        with pytest.raises(svc.AbroadModeDisabled):
            await svc.set_abroad_mode(_db(), FakePanel(), _sub(), True)


async def test_no_panel_account():
    sub = _sub()
    sub.remnawave_id = None
    with pytest.raises(svc.AbroadModeUnavailable):
        await svc.set_abroad_mode(_db(), FakePanel(), sub, True)


def test_sync_guard_keeps_abroad_over_tariff_squad():
    assert svc.resolve_external_squad_for_sync(_sub(mode=True), TARIFF_SQUAD) == ABROAD
    assert svc.resolve_external_squad_for_sync(_sub(mode=False), TARIFF_SQUAD) == TARIFF_SQUAD
    with patch.object(svc.settings, 'ABROAD_MODE_ENABLED', False):
        assert svc.resolve_external_squad_for_sync(_sub(mode=True), TARIFF_SQUAD) == TARIFF_SQUAD
