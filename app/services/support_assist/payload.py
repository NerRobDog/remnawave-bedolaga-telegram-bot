"""Нагрузка для LLM: копия снимка по белому списку.

Каждое поле, которое уходит в Anthropic API, названо здесь явно. Всё остальное,
что лежит в снимке (id, telegram_id, имена, e-mail, IP, ссылки подписки, uuid,
суммы и реквизиты платежей), не копируется, даже если появится в словарях
снимка позже. Новое поле попадает в нагрузку только правкой этого файла.

Текст самого тикета сюда не входит: он идёт в запрос отдельно и как есть.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import date, datetime
from typing import Any

from app.services.support_assist.flags import Flag
from app.services.support_assist.snapshot import RESTRICTION_KINDS, Snapshot
from app.services.support_assist.ticket_form import (
    FORM_FIELD_IDS,
    OTHER_OPTION,
    clean_other_text,
    has_other,
    looks_valid,
    other_key,
)


FORBIDDEN_KEYS: frozenset[str] = frozenset(
    {
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
    }
)

_DATE_KEY_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
_METHODS = frozenset({'GET', 'POST', 'PUT', 'PATCH', 'DELETE'})
# Сегмент пути кабинета, который можно показывать как есть: слово из строчных букв.
# Всё остальное (hwid устройства, токены, uuid) заменяется на {id}.
_SAFE_SEGMENT_RE = re.compile(r'^[a-z][a-z_-]{0,31}$')
_ID_PLACEHOLDER = '{id}'


def find_forbidden_keys(obj: Any) -> list[str]:
    """Обойти структуру рекурсивно и вернуть найденные запрещённые ключи (для тестов и самопроверок)."""
    found: list[str] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in FORBIDDEN_KEYS:
                found.append(str(key))
            found.extend(find_forbidden_keys(value))
    elif isinstance(obj, (list, tuple, set, frozenset)):
        for item in obj:
            found.extend(find_forbidden_keys(item))
    return found


def _iso(value: Any) -> str | None:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return None


def _pick(row: dict | None, plain: Iterable[str] = (), dates: Iterable[str] = ()) -> dict | None:
    """Скопировать из словаря только названные поля; даты перевести в ISO-строки."""
    if row is None:
        return None
    out: dict[str, Any] = {key: row.get(key) for key in plain}
    out.update({key: _iso(row.get(key)) for key in dates})
    return out


def _pick_all(rows: Iterable[dict], plain: Iterable[str] = (), dates: Iterable[str] = ()) -> list[dict]:
    plain, dates = tuple(plain), tuple(dates)
    return [_pick(row, plain, dates) for row in rows]  # type: ignore[misc]


def _safe_action(raw: Any) -> str:
    """``METHOD /path`` без непрозрачных сегментов.

    Кабинет сворачивает в ``{id}`` только числовые сегменты, поэтому
    ``DELETE /cabinet/subscription/devices/<hwid>`` приходит в снимок с реальным
    hwid устройства. В нагрузку идут только сегменты-слова.
    """
    method, _, path = str(raw or '')[:100].partition(' ')
    method = method.upper()
    if method not in _METHODS or not path.startswith('/'):
        return 'other'
    segments = [
        seg if seg == _ID_PLACEHOLDER or _SAFE_SEGMENT_RE.match(seg) else _ID_PLACEHOLDER for seg in path.split('/')[1:]
    ]
    return f'{method} /' + '/'.join(segments)


def _usage(by_day: dict) -> dict[str, int]:
    return {
        key: int(value)
        for key, value in by_day.items()
        if isinstance(key, str)
        and _DATE_KEY_RE.match(key)
        and isinstance(value, (int, float))
        and not isinstance(value, bool)
    }


def _diagnostics(raw: Any) -> dict[str, str] | None:
    """Ответы анкеты по белому списку: поля из ``FORM_FIELD_IDS``, текст «Другое» коротким.

    Это слова клиента из кнопок, а не измеренные факты. Значения не по форме (id платформы,
    название приложения до 40 знаков, статичные варианты вроде ``location``) и лишние ключи
    (что бы ни лежало в колонке тикета) в нагрузку не идут.
    """
    if not isinstance(raw, dict):
        return None
    out: dict[str, str] = {}
    for field in FORM_FIELD_IDS:
        value = raw.get(field)
        if not looks_valid(field, value):
            continue
        out[field] = value
        extra = raw.get(other_key(field))
        if value == OTHER_OPTION and has_other(field) and isinstance(extra, str) and clean_other_text(extra):
            out[other_key(field)] = clean_other_text(extra)
    return out or None


def build_payload(s: Snapshot, flags: list[Flag]) -> dict:
    """Собрать JSON-совместимую нагрузку для LLM по белому списку полей."""
    return {
        'as_of': _iso(s.as_of),
        'account': _pick(s.account, plain=('status', 'language', 'auth_type'), dates=('created_at',)),
        'subscription': _pick(
            s.subscription,
            plain=('status', 'tariff', 'device_limit', 'is_daily', 'is_daily_paused', 'is_trial'),
            dates=('end_date', 'last_revoke_at'),
        ),
        'panel_user': _pick(
            s.panel_user,
            plain=('status', 'hwid_limit', 'last_node_name', 'last_node_connected', 'last_node_disabled'),
            dates=('expire_at', 'online_at', 'first_connected_at'),
        ),
        'devices': _pick_all(s.devices, plain=('platform', 'os_version', 'device_model'), dates=('created_at',)),
        'sub_requests': _pick_all(s.sub_requests, plain=('user_agent',), dates=('request_at',)),
        'sub_requests_total': s.sub_requests_total,
        'usage_by_day': _usage(s.usage_by_day),
        'squad_nodes': _pick_all(s.squad_nodes, plain=('name', 'is_connected', 'is_disabled')),
        'cabinet_actions_7d': [
            {'action': _safe_action(a.get('button_id')), 'at': _iso(a.get('at'))} for a in s.cabinet_actions_7d
        ],
        'transactions_48h': _pick_all(s.transactions_48h, plain=('type', 'status'), dates=('at',)),
        'bot_errors_24h': s.bot_errors_24h,
        'past_tickets': _pick_all(s.past_tickets, plain=('status',), dates=('created_at',)),
        'other_tickets_2h': s.other_tickets_2h,
        'restrictions': [kind for kind in RESTRICTION_KINDS if kind in s.restrictions],
        'diagnostics': _diagnostics(s.diagnostics),
        'missing': [str(m) for m in s.missing],
        'flags': [{'code': f.code, 'sev': f.sev, 'text': f.text} for f in flags],
    }
