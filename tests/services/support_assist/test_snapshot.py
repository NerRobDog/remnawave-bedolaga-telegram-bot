import asyncio
import time
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.external.remnawave_api import RemnaWaveAPIError
from app.services.support_assist import snapshot as snap


NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
PANEL_USER = {
    'response': {
        'status': 'ACTIVE',
        'expireAt': '2026-12-01T00:00:00.000Z',
        'hwidDeviceLimit': 5,
        'userTraffic': {
            'onlineAt': '2026-11-06T11:40:00.000Z',
            'firstConnectedAt': '2026-09-01T00:00:00.000Z',
            'lastConnectedNodeUuid': 'n1',
        },
    }
}


def _patch_bot(monkeypatch):
    async def bot_data(db, ticket, now):
        return {
            'account': {'id': 7, 'language': 'ru', 'remnawave_id': 42},
            'subscription': {'status': 'active', 'end_date': NOW, 'tariff': 'T', 'device_limit': 5},
            'cabinet_actions_7d': [],
            'transactions_48h': [],
            'bot_errors_24h': 0,
            'past_tickets': [],
            'other_tickets_2h': 0,
            'restrictions': [],
        }

    monkeypatch.setattr(snap, '_load_bot_data', bot_data)


def _api(devices_exc=None):
    api = MagicMock()

    async def make_request(method, endpoint, data=None, params=None, **kw):
        assert method == 'GET'
        if endpoint == '/api/users/42':
            return PANEL_USER
        if endpoint == '/api/users/42/subscription-request-history':
            return {
                'response': {'total': 1, 'records': [{'requestAt': '2026-11-06T10:00:00.000Z', 'userAgent': 'X/1'}]}
            }
        if endpoint == '/api/users/42/accessible-nodes':
            return {'response': {'activeNodes': [{'uuid': 'n1', 'nodeName': 'node-a'}]}}
        raise AssertionError(endpoint)

    api._make_request = AsyncMock(side_effect=make_request)
    node = MagicMock(uuid='n1', is_connected=True, is_disabled=False)
    node.name = 'node-a'
    api.get_all_nodes = AsyncMock(return_value=[node])
    api.get_user_stats_usage = AsyncMock(return_value={'categories': ['2026-11-05'], 'sparklineData': [123]})
    if devices_exc:
        api.get_user_devices = AsyncMock(side_effect=devices_exc)
    else:
        api.get_user_devices = AsyncMock(return_value={'devices': [], 'total': 0})
    return api


@pytest.mark.asyncio
async def test_collect_snapshot_happy(monkeypatch):
    _patch_bot(monkeypatch)
    s = await snap.collect_snapshot(AsyncMock(), _api(), MagicMock(user_id=7, id=1), NOW)
    assert s.missing == []
    assert s.panel_user['last_node_name'] == 'node-a'
    assert s.sub_requests_total == 1
    assert s.usage_by_day == {'2026-11-05': 123}
    assert s.squad_nodes == [{'name': 'node-a', 'is_connected': True, 'is_disabled': False}]


@pytest.mark.asyncio
async def test_collect_snapshot_devices_failure_is_isolated(monkeypatch):
    _patch_bot(monkeypatch)
    s = await snap.collect_snapshot(
        AsyncMock(), _api(devices_exc=RemnaWaveAPIError('boom', 500)), MagicMock(user_id=7, id=1), NOW
    )
    assert 'devices' in s.missing
    assert s.panel_user is not None and s.sub_requests and s.squad_nodes


@pytest.mark.asyncio
async def test_history_failure_goes_to_missing_not_empty(monkeypatch):
    _patch_bot(monkeypatch)
    api = _api()
    orig = api._make_request.side_effect

    async def flaky(method, endpoint, *a, **kw):
        if endpoint.endswith('subscription-request-history'):
            raise RemnaWaveAPIError('down', 502)
        return await orig(method, endpoint, *a, **kw)

    api._make_request = AsyncMock(side_effect=flaky)
    s = await snap.collect_snapshot(AsyncMock(), api, MagicMock(user_id=7, id=1), NOW)
    assert 'sub_requests' in s.missing
    assert s.sub_requests_total is None


@pytest.mark.asyncio
async def test_no_remnawave_id_means_no_panel_user(monkeypatch):
    async def bot_data(db, ticket, now):
        return {'account': {'id': 7, 'remnawave_id': None}, 'subscription': None}

    monkeypatch.setattr(snap, '_load_bot_data', bot_data)
    api = _api()
    s = await snap.collect_snapshot(AsyncMock(), api, MagicMock(user_id=7, id=1), NOW)
    assert s.panel_user is None
    assert s.missing == []
    api._make_request.assert_not_called()


@pytest.mark.asyncio
async def test_bot_db_failure_is_isolated(monkeypatch):
    async def boom(db, ticket, now):
        raise RuntimeError('db')

    monkeypatch.setattr(snap, '_load_bot_data', boom)
    s = await snap.collect_snapshot(AsyncMock(), _api(), MagicMock(user_id=7, id=1), NOW)
    assert 'bot_db' in s.missing


# --- соединение с БД и зависшая панель ------------------------------------------


@pytest.mark.asyncio
async def test_transaction_is_ended_before_any_panel_call(monkeypatch):
    """Чтение из БД бота открывает транзакцию; до сетевых вызовов она должна закрыться."""
    _patch_bot(monkeypatch)
    db = AsyncMock()
    api = _api()
    orig = api._make_request.side_effect
    commits_seen = []

    async def spy(method, endpoint, *a, **kw):
        commits_seen.append(db.commit.await_count)
        return await orig(method, endpoint, *a, **kw)

    api._make_request = AsyncMock(side_effect=spy)
    s = await snap.collect_snapshot(db, api, MagicMock(user_id=7, id=1), NOW)
    assert s.missing == []
    assert commits_seen and all(n >= 1 for n in commits_seen)


@pytest.mark.asyncio
async def test_failed_bot_db_session_is_rolled_back_before_panel_calls(monkeypatch):
    async def boom(db, ticket, now):
        raise RuntimeError('db')

    monkeypatch.setattr(snap, '_load_bot_data', boom)
    db = AsyncMock()
    db.commit.side_effect = RuntimeError('PendingRollbackError')
    s = await snap.collect_snapshot(db, _api(), MagicMock(user_id=7, id=1), NOW)
    assert s.missing == ['bot_db']
    db.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_hanging_panel_source_goes_to_missing_not_forever(monkeypatch):
    _patch_bot(monkeypatch)
    monkeypatch.setattr(snap, 'PANEL_SOURCE_TIMEOUT_SECONDS', 0.05)
    api = _api()
    orig = api._make_request.side_effect

    async def hang_history(method, endpoint, *a, **kw):
        if endpoint.endswith('subscription-request-history'):
            await asyncio.sleep(3600)
        return await orig(method, endpoint, *a, **kw)

    api._make_request = AsyncMock(side_effect=hang_history)
    s = await asyncio.wait_for(snap.collect_snapshot(AsyncMock(), api, MagicMock(user_id=7, id=1), NOW), 5)
    assert s.missing == ['sub_requests']
    assert s.sub_requests_total is None
    assert s.panel_user is not None and s.squad_nodes  # остальные источники не пострадали


@pytest.mark.asyncio
async def test_panel_sources_run_concurrently_and_total_wait_is_bounded(monkeypatch):
    """Шесть зависших источников ждут один таймаут, а не шесть подряд."""
    _patch_bot(monkeypatch)
    monkeypatch.setattr(snap, 'PANEL_SOURCE_TIMEOUT_SECONDS', 0.2)
    api = MagicMock()

    async def hang(*a, **kw):
        await asyncio.sleep(3600)

    api._make_request = AsyncMock(side_effect=hang)
    api.get_user_devices = AsyncMock(side_effect=hang)
    api.get_user_stats_usage = AsyncMock(side_effect=hang)
    api.get_all_nodes = AsyncMock(side_effect=hang)

    started = time.monotonic()
    s = await snap.collect_snapshot(AsyncMock(), api, MagicMock(user_id=7, id=1), NOW)
    elapsed = time.monotonic() - started
    assert set(s.missing) == {'panel_user', 'sub_requests', 'devices', 'usage', 'squad_nodes', 'nodes'}
    assert elapsed < 0.2 * 3  # последовательно вышло бы 1.2 с


FORM = {'platform': 'ios', 'app': 'happ', 'network': 'wifi', 'symptom': 'nothing', 'since': 'today'}


@pytest.mark.asyncio
async def test_snapshot_carries_the_ticket_form_answers(monkeypatch):
    _patch_bot(monkeypatch)
    ticket = MagicMock(user_id=7, id=1, diagnostics=dict(FORM))
    s = await snap.collect_snapshot(AsyncMock(), _api(), ticket, NOW)
    assert s.diagnostics == FORM
    assert s.diagnostics is not ticket.diagnostics  # копия: откат сессии тикет не трогает снимок


@pytest.mark.asyncio
async def test_snapshot_keeps_the_form_answers_when_the_bot_db_source_fails(monkeypatch):
    async def boom(db, ticket, now):
        raise RuntimeError('db down')

    monkeypatch.setattr(snap, '_load_bot_data', boom)
    ticket = MagicMock(user_id=7, id=1, diagnostics=dict(FORM))
    s = await snap.collect_snapshot(AsyncMock(), _api(), ticket, NOW)
    assert 'bot_db' in s.missing
    assert s.diagnostics == FORM


@pytest.mark.asyncio
async def test_snapshot_without_a_form_has_no_diagnostics(monkeypatch):
    _patch_bot(monkeypatch)
    for value in (None, 'x', ['ios']):
        s = await snap.collect_snapshot(AsyncMock(), _api(), MagicMock(user_id=7, id=1, diagnostics=value), NOW)
        assert s.diagnostics is None
