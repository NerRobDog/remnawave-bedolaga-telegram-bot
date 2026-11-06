"""Синтетические снимки S01-S15 для проверки флагов (только выдуманные значения)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from app.services.support_assist.snapshot import Snapshot


AS_OF = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
MB = 1024 * 1024


def _ago(**kw) -> datetime:
    return AS_OF - timedelta(**kw)


def _ahead(**kw) -> datetime:
    return AS_OF + timedelta(**kw)


def prev_day() -> str:
    return (AS_OF.date() - timedelta(days=1)).isoformat()


def healthy(**over) -> Snapshot:
    """Здоровый клиент: ничего не должно срабатывать выше info."""
    data = dict(
        as_of=AS_OF,
        account={'id': 9001, 'status': 'active', 'language': 'ru'},
        subscription={
            'status': 'active',
            'end_date': _ahead(days=20),
            'tariff': 'Base',
            'device_limit': 5,
            'is_daily': False,
            'is_daily_paused': False,
            'is_trial': False,
            'last_revoke_at': None,
        },
        panel_user={
            'status': 'ACTIVE',
            'expire_at': _ahead(days=20),
            'hwid_limit': 5,
            'online_at': _ago(minutes=20),
            'last_node_name': 'node-a',
            'last_node_connected': True,
            'last_node_disabled': False,
            'first_connected_at': _ago(days=60),
        },
        devices=[
            {
                'platform': 'iOS',
                'os_version': '18.6',
                'device_model': 'iPhone15,2',
                'user_agent': 'ClashMi/1.0.17',
                'created_at': _ago(days=50),
            },
            {
                'platform': 'macOS',
                'os_version': '15.5',
                'device_model': 'MacBookAir10,1',
                'user_agent': 'ClashMi/1.0.17',
                'created_at': _ago(days=40),
            },
        ],
        sub_requests=[{'request_at': _ago(hours=h), 'user_agent': 'ClashMi/1.0.17'} for h in range(1, 11)],
        sub_requests_total=10,
        usage_by_day={prev_day(): 900 * MB, AS_OF.date().isoformat(): 100 * MB},
        squad_nodes=[
            {'name': 'node-a', 'is_connected': True, 'is_disabled': False},
            {'name': 'node-b', 'is_connected': True, 'is_disabled': False},
        ],
        cabinet_actions_7d=[],
        transactions_48h=[],
        bot_errors_24h=0,
        past_tickets=[],
        other_tickets_2h=0,
        restrictions=[],
        missing=[],
    )
    data.update(over)
    return Snapshot(**data)


def _with(base_key: str, **patch) -> dict:
    base = healthy()
    merged = dict(getattr(base, base_key))
    merged.update(patch)
    return merged


def s01() -> Snapshot:
    return healthy(
        sub_requests=[],
        sub_requests_total=0,
        usage_by_day={},
        devices=[],
        panel_user=_with('panel_user', online_at=None, last_node_name=None, first_connected_at=None),
    )


def s02() -> Snapshot:
    return healthy(
        usage_by_day={},
        panel_user=_with('panel_user', online_at=None, last_node_name=None, first_connected_at=None),
    )


def s03() -> Snapshot:
    devs = healthy().devices + [
        {
            'platform': 'Android',
            'os_version': '14',
            'device_model': 'SM-S918B',
            'user_agent': 'ClashMi/1.0.17',
            'created_at': _ago(days=d),
        }
        for d in (3, 4, 5)
    ]
    return healthy(devices=devs)


def s04() -> Snapshot:
    revoke_at = _ago(minutes=15)
    return healthy(
        subscription=_with('subscription', last_revoke_at=revoke_at),
        cabinet_actions_7d=[
            {'button_id': 'POST /cabinet/subscription/revoke', 'at': _ago(minutes=m)} for m in (15, 70, 130)
        ],
        sub_requests=[{'request_at': _ago(hours=h), 'user_agent': 'ClashMi/1.0.17'} for h in range(3, 13)],
        usage_by_day={},
    )


def s05() -> Snapshot:
    return healthy(
        subscription=_with('subscription', status='expired', end_date=_ago(hours=20)),
        panel_user=_with('panel_user', status='EXPIRED', expire_at=_ago(hours=20)),
        usage_by_day={},
    )


def s06() -> Snapshot:
    return healthy(
        subscription=_with('subscription', end_date=_ahead(days=30)),
        panel_user=_with('panel_user', status='EXPIRED', expire_at=_ago(hours=2)),
        transactions_48h=[{'type': 'subscription_payment', 'status': 'completed', 'at': _ago(hours=1)}],
        usage_by_day={},
    )


def s07() -> Snapshot:
    return healthy(
        panel_user=_with('panel_user', last_node_name='node-a', last_node_connected=False),
        squad_nodes=[
            {'name': 'node-a', 'is_connected': False, 'is_disabled': False},
            {'name': 'node-b', 'is_connected': True, 'is_disabled': False},
        ],
    )


def s08() -> Snapshot:
    return healthy(
        panel_user=_with('panel_user', last_node_name='node-c', last_node_connected=False),
        squad_nodes=[{'name': 'node-c', 'is_connected': False, 'is_disabled': False}],
        usage_by_day={},
    )


def s09() -> Snapshot:
    return healthy(
        sub_requests=[
            {'request_at': _ago(hours=h), 'user_agent': 'Happ/3.2.1'}
            for h in range(1, 11)
        ],
    )


def s10() -> Snapshot:
    return healthy(
        subscription=_with('subscription', is_daily=True, is_daily_paused=True),
        panel_user=_with('panel_user', status='DISABLED'),
        usage_by_day={},
    )


def s11() -> Snapshot:
    return healthy(restrictions=['restriction_subscription: chargeback'])


def s12() -> Snapshot:
    return healthy(
        panel_user=_with('panel_user', last_node_name='node-b', last_node_connected=False),
        squad_nodes=[
            {'name': 'node-a', 'is_connected': True, 'is_disabled': False},
            {'name': 'node-b', 'is_connected': False, 'is_disabled': False},
        ],
        other_tickets_2h=3,
    )


def s13() -> Snapshot:
    return healthy(transactions_48h=[{'type': 'deposit', 'status': 'pending', 'at': _ago(minutes=40)}])


def s14() -> Snapshot:
    return healthy()


def s15() -> Snapshot:
    return s01()


# code -> (builder, must contain, must not contain)
SCENARIOS: dict[str, tuple[Callable[[], Snapshot], set[str], set[str]]] = {
    'S01': (s01, {'never_fetched', 'never_connected'}, set()),
    'S02': (s02, {'never_connected'}, {'never_fetched'}),
    'S03': (s03, {'device_limit_full'}, set()),
    'S04': (s04, {'revoked_recently', 'not_refetched_after_revoke'}, set()),
    'S05': (s05, {'sub_expired'}, set()),
    'S06': (s06, {'desync_status', 'desync_expire'}, set()),
    'S07': (s07, {'last_node_down', 'some_squad_nodes_down'}, {'all_squad_nodes_down'}),
    'S08': (s08, {'last_node_down', 'all_squad_nodes_down'}, set()),
    'S09': (s09, {'happ_base64'}, set()),
    'S10': (s10, {'daily_paused'}, {'desync_status'}),
    'S11': (s11, {'restricted'}, set()),
    'S12': (s12, {'last_node_down', 'mass_tickets'}, set()),
    'S13': (s13, {'tx_pending'}, set()),
    'S14': (s14, set(), {'never_fetched', 'never_connected', 'device_limit_full', 'sub_expired', 'last_node_down'}),
    'S15': (s15, {'never_fetched'}, set()),
}
