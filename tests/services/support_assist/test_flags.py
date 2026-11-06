from dataclasses import FrozenInstanceError

import pytest

from app.services.support_assist.flags import Flag, compute_flags
from tests.services.support_assist.fixtures import SCENARIOS, healthy, prev_day


HIGH_MID = {'high', 'mid'}


def _codes(flags, sevs=None):
    return {f.code for f in flags if sevs is None or f.sev in sevs}


@pytest.mark.parametrize('name', sorted(SCENARIOS))
def test_scenario_flags(name):
    build, must, must_not = SCENARIOS[name]
    flags = compute_flags(build())
    codes = _codes(flags)
    assert must <= codes, (name, codes)
    assert not (must_not & codes), (name, codes)
    assert all(isinstance(f, Flag) for f in flags)


def test_healthy_has_no_high_or_mid():
    assert _codes(compute_flags(healthy()), HIGH_MID) == set()


def test_disabled_node_not_down():
    s = healthy(
        panel_user={**healthy().panel_user, 'last_node_connected': False, 'last_node_disabled': True},
        squad_nodes=[
            {'name': 'node-a', 'is_connected': False, 'is_disabled': True},
            {'name': 'node-b', 'is_connected': False, 'is_disabled': True},
        ],
    )
    codes = _codes(compute_flags(s))
    assert not ({'last_node_down', 'all_squad_nodes_down', 'some_squad_nodes_down'} & codes)


def test_history_truncated_gives_unavailable_not_never_fetched():
    s = healthy(sub_requests=[], sub_requests_total=57)
    codes = _codes(compute_flags(s))
    assert 'sub_history_unavailable' in codes
    assert 'never_fetched' not in codes


def test_unknown_total_gives_unavailable_not_never_fetched():
    s = healthy(sub_requests=[], sub_requests_total=None, missing=['sub_requests'])
    codes = _codes(compute_flags(s))
    assert 'sub_history_unavailable' in codes
    assert 'never_fetched' not in codes


def test_no_subscription_snapshot():
    s = healthy(
        subscription=None,
        panel_user=None,
        devices=[],
        sub_requests=[],
        sub_requests_total=None,
        usage_by_day={},
        squad_nodes=[],
    )
    assert _codes(compute_flags(s)) == {'no_subscription'}


def test_daily_paused_suppresses_desync_status():
    s = SCENARIOS['S10'][0]()
    assert 'desync_status' not in _codes(compute_flags(s))


def test_traffic_uses_previous_full_day():
    # traffic only today (partial day) must not count as "yesterday traffic"
    s = healthy(usage_by_day={'2026-11-06': 500 * 1024 * 1024})
    codes = _codes(compute_flags(s))
    assert 'zero_traffic' in codes
    s2 = healthy(usage_by_day={prev_day(): 5 * 1024 * 1024})
    codes2 = _codes(compute_flags(s2))
    assert 'traffic_flows' in codes2 and 'zero_traffic' not in codes2


def test_missing_usage_does_not_claim_zero_traffic():
    s = healthy(usage_by_day={}, missing=['usage'])
    assert 'zero_traffic' not in _codes(compute_flags(s))


def test_sub_expiring_soon():
    s = healthy(subscription={**healthy().subscription, 'end_date': healthy().as_of.replace(hour=20)})
    assert 'sub_expiring' in _codes(compute_flags(s))


def test_flag_is_frozen():
    f = Flag(code='x', sev='low', text='t')
    with pytest.raises(FrozenInstanceError):
        f.code = 'y'  # type: ignore[misc]
