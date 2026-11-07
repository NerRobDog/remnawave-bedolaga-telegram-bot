import json

import pytest

from app.services.support_assist.flags import Flag, compute_flags
from app.services.support_assist.payload import FORBIDDEN_KEYS, build_payload, find_forbidden_keys
from app.services.support_assist.snapshot import Snapshot
from tests.services.support_assist.fixtures import AS_OF, LEAK, SCENARIOS, healthy, leaky_snapshot, prev_day


def _walk_strings(obj):
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield str(key)
            yield from _walk_strings(value)
    elif isinstance(obj, (list, tuple, set)):
        for item in obj:
            yield from _walk_strings(item)
    elif isinstance(obj, str):
        yield obj


def test_forbidden_keys_cover_the_privacy_list():
    assert {
        'id',
        'user_id',
        'telegram_id',
        'username',
        'first_name',
        'last_name',
        'full_name',
        'email',
        'ip',
        'request_ip',
        'ips',
        'subscription_url',
        'sub_url',
        'short_uuid',
        'uuid',
        'remnawave_id',
        'amount',
        'amount_kopeks',
        'payment_id',
        'card',
        'phone',
    } == set(FORBIDDEN_KEYS)
    assert isinstance(FORBIDDEN_KEYS, frozenset)


def test_walker_finds_nested_forbidden_keys():
    assert find_forbidden_keys({'a': [{'b': {'uuid': 1}}], 'ok': 2}) == ['uuid']
    assert find_forbidden_keys({'a': [{'b': 1}]}) == []


def test_payload_has_no_forbidden_keys():
    snap = leaky_snapshot()
    # маркер действительно лежит в каждом словаре снимка
    assert snap.account['telegram_id'] == LEAK
    assert snap.devices[0]['uuid'] == LEAK
    assert snap.transactions_48h[0]['amount_kopeks'] == LEAK

    payload = build_payload(snap, compute_flags(snap))

    assert find_forbidden_keys(payload) == []
    strings = list(_walk_strings(payload))
    assert not [s for s in strings if LEAK in s]
    assert LEAK not in json.dumps(payload, ensure_ascii=False)


@pytest.mark.parametrize('name', sorted(SCENARIOS))
def test_every_scenario_payload_is_clean_and_json_serialisable(name):
    snap = SCENARIOS[name][0]()
    payload = build_payload(snap, compute_flags(snap))
    assert find_forbidden_keys(payload) == []
    json.dumps(payload)  # даты уже строки, объектов datetime не осталось


def test_payload_keeps_device_model_and_flags():
    snap = healthy()
    flags = [Flag('device_limit_full', 'high', 'Устройств 5 из 5'), Flag('x', 'info', 'y')]
    payload = build_payload(snap, flags)

    first = payload['devices'][0]
    assert first['device_model'] == 'iPhone15,2'
    assert first['platform'] == 'iOS'
    assert first['os_version'] == '18.6'
    assert first['created_at'] == snap.devices[0]['created_at'].isoformat()
    assert set(first) == {'platform', 'os_version', 'device_model', 'created_at'}
    assert payload['flags'] == [
        {'code': 'device_limit_full', 'sev': 'high', 'text': 'Устройств 5 из 5'},
        {'code': 'x', 'sev': 'info', 'text': 'y'},
    ]


def test_dates_are_iso_strings():
    snap = healthy()
    payload = build_payload(snap, [])
    assert payload['as_of'] == AS_OF.isoformat()
    assert payload['subscription']['end_date'] == snap.subscription['end_date'].isoformat()
    assert payload['panel_user']['online_at'] == snap.panel_user['online_at'].isoformat()
    assert payload['panel_user']['first_connected_at'] == snap.panel_user['first_connected_at'].isoformat()
    assert payload['sub_requests'][0]['request_at'] == snap.sub_requests[0]['request_at'].isoformat()


def test_transactions_keep_only_type_status_at():
    snap = leaky_snapshot()
    payload = build_payload(snap, [])
    assert payload['transactions_48h'] == [
        {'type': 'deposit', 'status': 'pending', 'at': snap.transactions_48h[0]['at'].isoformat()}
    ]


def test_account_and_subscription_drop_identifiers():
    snap = leaky_snapshot()
    payload = build_payload(snap, [])
    assert set(payload['account']) == {'status', 'language', 'auth_type', 'created_at'}
    assert 'remnawave_id' not in payload['subscription']
    assert payload['subscription']['tariff'] == 'Base'
    assert payload['subscription']['device_limit'] == 5
    assert payload['panel_user']['hwid_limit'] == 5
    assert payload['panel_user']['last_node_name'] == 'node-a'


def test_past_tickets_have_no_ticket_id():
    payload = build_payload(leaky_snapshot(), [])
    assert payload['past_tickets'] == [
        {'created_at': leaky_snapshot().past_tickets[0]['created_at'].isoformat(), 'status': 'closed'}
    ]


def test_sub_requests_keep_time_and_app_only():
    payload = build_payload(leaky_snapshot(), [])
    assert set(payload['sub_requests'][0]) == {'request_at', 'user_agent'}
    assert payload['sub_requests'][0]['user_agent'] == 'ClashMi/1.0.17'
    assert payload['sub_requests_total'] == 10


def test_usage_by_day_keeps_only_dates_with_numeric_values():
    payload = build_payload(leaky_snapshot(), [])
    assert set(payload['usage_by_day']) == {prev_day(), AS_OF.date().isoformat()}
    assert all(isinstance(v, int) for v in payload['usage_by_day'].values())


def test_cabinet_action_path_loses_opaque_segments():
    """Числовые id кабинет сворачивает сам, а hwid устройства в пути остаётся: прятать надо здесь."""
    snap = healthy(
        cabinet_actions_7d=[
            {'button_id': 'DELETE /cabinet/subscription/devices/9f86d081884c7d65', 'at': AS_OF},
            {'button_id': 'DELETE /cabinet/subscription/devices/{id}', 'at': AS_OF},
            {'button_id': 'POST /cabinet/subscription/revoke', 'at': AS_OF},
            {'button_id': 'POST /cabinet/gift/Zm9vYmFyLXRva2Vu', 'at': AS_OF},
            {'button_id': 'SCREEN /cabinet/whatever', 'at': AS_OF},
        ]
    )
    actions = [a['action'] for a in build_payload(snap, [])['cabinet_actions_7d']]
    assert actions == [
        'DELETE /cabinet/subscription/devices/{id}',
        'DELETE /cabinet/subscription/devices/{id}',
        'POST /cabinet/subscription/revoke',
        'POST /cabinet/gift/{id}',
        'other',
    ]
    assert '9f86d081884c7d65' not in json.dumps(build_payload(snap, []))


def test_missing_sections_stay_none_or_empty():
    snap = Snapshot(as_of=AS_OF, missing=['bot_db'])
    payload = build_payload(snap, [])
    assert payload['account'] is None
    assert payload['subscription'] is None
    assert payload['panel_user'] is None
    assert payload['devices'] == []
    assert payload['sub_requests_total'] is None
    assert payload['missing'] == ['bot_db']
    assert payload['flags'] == []
    json.dumps(payload)


def test_flag_text_built_from_leaky_snapshot_carries_no_marker():
    snap = leaky_snapshot()
    flags = compute_flags(snap)
    assert flags, 'нужен хотя бы один флаг, иначе тест ничего не проверяет'
    payload = build_payload(snap, flags)
    assert payload['flags']
    assert LEAK not in json.dumps(payload['flags'], ensure_ascii=False)


# --- анкета перед тикетом ----------------------------------------------------

FORM_ANSWERS = {'platform': 'ios', 'app': 'happ', 'network': 'wifi', 'symptom': 'nothing', 'since': 'today'}


def test_payload_includes_the_form_answers_and_stays_clean():
    snap = healthy(diagnostics=dict(FORM_ANSWERS))
    payload = build_payload(snap, [])
    assert payload['diagnostics'] == FORM_ANSWERS
    assert find_forbidden_keys(payload) == []
    json.dumps(payload)


def test_payload_diagnostics_is_none_without_a_form():
    assert build_payload(healthy(), [])['diagnostics'] is None
    assert build_payload(healthy(diagnostics={}), [])['diagnostics'] is None


def test_payload_diagnostics_is_an_allowlist():
    """Лишние ключи и значения вне списка вариантов в нагрузку не попадают, даже если лежат в тикете."""
    dirty = {
        **FORM_ANSWERS,
        'telegram_id': LEAK,
        'email': LEAK,
        'network': 'no_such_option',
        'platform_other': LEAK,  # ``other`` не выбран: текст не идёт
    }
    payload = build_payload(healthy(diagnostics=dirty), [])
    assert payload['diagnostics'] == {k: v for k, v in FORM_ANSWERS.items() if k != 'network'}
    assert LEAK not in json.dumps(payload, ensure_ascii=False)
    assert find_forbidden_keys(payload) == []


def test_payload_diagnostics_keeps_the_short_other_text_only_when_other_was_chosen():
    snap = healthy(diagnostics={**FORM_ANSWERS, 'platform': 'other', 'platform_other': 'Steam Deck' + 'x' * 200})
    out = build_payload(snap, [])['diagnostics']
    assert out['platform'] == 'other'
    assert out['platform_other'].startswith('Steam Deck')
    assert len(out['platform_other']) == 100


def test_payload_diagnostics_survives_garbage_in_the_column():
    for garbage in ('x', 5, ['ios'], {'platform': ['ios']}):
        payload = build_payload(healthy(diagnostics=garbage), [])
        assert payload['diagnostics'] in (None, {})
        json.dumps(payload)


def test_restrictions_in_payload_and_flag_text_are_kinds_only():
    """Даже если в снимок попал текст причины (@ник, id), в нагрузку к LLM уходят только виды запретов."""
    reason = 'subscription: chargeback @leakuser 123456789'
    snap = healthy(restrictions=['subscription', 'topup', reason])

    flags = compute_flags(snap)
    payload = build_payload(snap, flags)

    assert payload['restrictions'] == ['subscription', 'topup']
    dumped = json.dumps(payload, ensure_ascii=False)
    assert 'leakuser' not in dumped
    assert '123456789' not in dumped
    restricted = next(f for f in flags if f.code == 'restricted')
    assert 'leakuser' not in restricted.text
    assert 'продления' in restricted.text
    assert 'пополнения' in restricted.text
