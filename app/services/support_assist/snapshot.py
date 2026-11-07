"""Снимок состояния клиента для ассистента поддержки.

Только чтение: SELECT из БД бота и GET-запросы к панели Remnawave. Каждый
источник изолирован, сбой одного попадает в ``Snapshot.missing`` и не роняет
остальные. Вызовы идут через сырой ``_make_request``: готовые методы
``RemnaWaveAPI`` либо глотают ошибки (история запросов подписки превратилась бы
в ложное «ни разу не забирала»), либо тянут Happ-ссылки (``get_user_by_id``).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.support_assist import end_transaction
from app.database.models import (
    ButtonClickLog,
    Subscription,
    SystemErrorEvent,
    Tariff,
    Ticket,
    Transaction,
    User,
)
from app.external.remnawave_api import RemnaWaveAPI
from app.external.remnawave_errors import coerce_panel_user_id


logger = structlog.get_logger(__name__)

_ACTION_PREFIXES = ('POST%', 'DELETE%', 'PATCH%')

# Виды запретов аккаунта, единственное, что снимок говорит о ``restriction_*``.
RESTRICTION_KINDS = ('subscription', 'topup')

# Потолок на один запрос к панели вместе с её повторами. Источники идут параллельно, поэтому
# зависшая панель задерживает снимок на это время один раз, а не по разу на каждый источник.
PANEL_SOURCE_TIMEOUT_SECONDS = 20.0

# Порядок источников в ``Snapshot.missing``: параллельные запросы завершаются в произвольном порядке.
_SOURCE_ORDER = ('bot_db', 'panel_user', 'sub_requests', 'devices', 'usage', 'squad_nodes', 'nodes')


@dataclass
class Snapshot:
    as_of: datetime
    account: dict | None = None
    subscription: dict | None = None
    panel_user: dict | None = None
    devices: list[dict] = field(default_factory=list)
    sub_requests: list[dict] = field(default_factory=list)
    sub_requests_total: int | None = None
    usage_by_day: dict[str, int] = field(default_factory=dict)
    squad_nodes: list[dict] = field(default_factory=list)
    cabinet_actions_7d: list[dict] = field(default_factory=list)
    transactions_48h: list[dict] = field(default_factory=list)
    bot_errors_24h: int = 0
    past_tickets: list[dict] = field(default_factory=list)
    other_tickets_2h: int = 0
    # Виды запретов из ``RESTRICTION_KINDS``; причину (текст админа) не хранит.
    restrictions: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    # Ответы обязательной анкеты из тикета (id вариантов); None — анкеты нет (старый тикет, флаг выключен).
    diagnostics: dict | None = None


def _dt(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


async def _guard[T](
    name: str, missing: list[str], fn: Callable[[], Awaitable[T]], timeout: float | None = None
) -> T | None:
    """Выполнить источник; при любой ошибке или превышении ``timeout`` записать его имя в ``missing``."""
    try:
        async with asyncio.timeout(timeout):
            return await fn()
    except Exception as exc:
        logger.warning('support_assist: источник снимка недоступен', source=name, error=repr(exc))
        missing.append(name)
        return None


async def _load_bot_data(db: AsyncSession, ticket: Ticket, now: datetime) -> dict[str, Any]:
    """Всё, что живёт в БД бота. Только SELECT."""
    uid = ticket.user_id
    user = (await db.execute(select(User).where(User.id == uid))).scalar_one_or_none()
    account = (
        {
            'id': user.id,
            'status': user.status,
            'language': user.language,
            'auth_type': user.auth_type,
            'remnawave_id': user.remnawave_id,
            'created_at': user.created_at,
        }
        if user
        else None
    )
    # Только виды запретов: ``restriction_reason`` — свободный текст админа (там бывают @ники и id), в снимок не идёт.
    restrictions: list[str] = []
    if user:
        if user.restriction_subscription:
            restrictions.append('subscription')
        if user.restriction_topup:
            restrictions.append('topup')

    row = (
        await db.execute(
            select(Subscription, Tariff.name, Tariff.is_daily)
            .outerjoin(Tariff, Tariff.id == Subscription.tariff_id)
            .where(Subscription.user_id == uid)
            .order_by(Subscription.id.desc())
            .limit(1)
        )
    ).first()
    subscription = None
    if row:
        sub, tariff_name, tariff_daily = row
        subscription = {
            'status': sub.status,
            'end_date': sub.end_date,
            'tariff': tariff_name,
            'device_limit': sub.device_limit,
            'is_daily': bool(tariff_daily),
            'is_daily_paused': bool(sub.is_daily_paused),
            'is_trial': bool(sub.is_trial),
            'last_revoke_at': sub.last_revoke_at,
            'remnawave_id': sub.remnawave_id,
        }
        if account is not None and subscription['remnawave_id']:
            account['remnawave_id'] = account['remnawave_id'] or subscription['remnawave_id']

    clicks = (
        await db.execute(
            select(ButtonClickLog.button_id, ButtonClickLog.clicked_at)
            .where(
                ButtonClickLog.user_id == uid,
                ButtonClickLog.clicked_at >= now - timedelta(days=7),
                ButtonClickLog.clicked_at <= now,
                or_(*[ButtonClickLog.button_id.ilike(p) for p in _ACTION_PREFIXES]),
            )
            .order_by(ButtonClickLog.clicked_at.desc())
            .limit(30)
        )
    ).all()

    txs = (
        await db.execute(
            select(Transaction.type, Transaction.is_completed, Transaction.created_at)
            .where(Transaction.user_id == uid, Transaction.created_at >= now - timedelta(hours=48))
            .order_by(Transaction.created_at.desc())
        )
    ).all()

    errors = (
        await db.execute(
            select(func.count())
            .select_from(SystemErrorEvent)
            .where(SystemErrorEvent.user_id == uid, SystemErrorEvent.created_at >= now - timedelta(hours=24))
        )
    ).scalar_one()

    past = (
        await db.execute(
            select(Ticket.id, Ticket.created_at, Ticket.status)
            .where(Ticket.user_id == uid, Ticket.id != ticket.id)
            .order_by(Ticket.id.desc())
            .limit(5)
        )
    ).all()

    others = (
        await db.execute(
            select(func.count())
            .select_from(Ticket)
            .where(Ticket.user_id != uid, Ticket.created_at >= now - timedelta(hours=2), Ticket.created_at <= now)
        )
    ).scalar_one()

    return {
        'account': account,
        'subscription': subscription,
        'cabinet_actions_7d': [{'button_id': b, 'at': at} for b, at in clicks],
        'transactions_48h': [
            {'type': t, 'status': 'completed' if done else 'pending', 'at': at} for t, done, at in txs
        ],
        'bot_errors_24h': int(errors or 0),
        'past_tickets': [{'id': i, 'created_at': at, 'status': st} for i, at, st in past],
        'other_tickets_2h': int(others or 0),
        'restrictions': restrictions,
    }


async def _fetch_panel_user(api: RemnaWaveAPI, panel_id: int) -> dict[str, Any]:
    """Сырой GET: берём только статус, срок, лимит и userTraffic (без Happ-обогащения)."""
    resp = (await api._make_request('GET', f'/api/users/{panel_id}', retry_transient=False)).get('response') or {}
    traffic = resp.get('userTraffic') or {}
    return {
        'status': resp.get('status'),
        'expire_at': _dt(resp.get('expireAt')),
        'hwid_limit': resp.get('hwidDeviceLimit'),
        'online_at': _dt(traffic.get('onlineAt')),
        'first_connected_at': _dt(traffic.get('firstConnectedAt')),
        '_last_node_uuid': traffic.get('lastConnectedNodeUuid'),
    }


async def _fetch_history(api: RemnaWaveAPI, panel_id: int) -> tuple[int | None, list[dict]]:
    resp = (
        await api._make_request('GET', f'/api/users/{panel_id}/subscription-request-history', retry_transient=False)
    ).get('response') or {}
    records = [
        {'request_at': _dt(r.get('requestAt')), 'user_agent': r.get('userAgent')}
        for r in resp.get('records') or []
        if _dt(r.get('requestAt'))
    ]
    total = resp.get('total')
    # Нет числа от панели — «неизвестно», а не «ноль»: иначе флаги заявят «ни разу не забирала».
    return (total if isinstance(total, int) and not isinstance(total, bool) else None), records


async def _fetch_devices(api: RemnaWaveAPI, panel_id: int) -> list[dict]:
    resp = await api.get_user_devices(panel_id)
    return [
        {
            'platform': d.get('platform'),
            'os_version': d.get('osVersion'),
            'device_model': d.get('deviceModel'),
            'user_agent': d.get('userAgent'),
            'created_at': _dt(d.get('createdAt')),
        }
        for d in resp.get('devices') or []
    ]


async def _fetch_usage(api: RemnaWaveAPI, panel_id: int, now: datetime) -> dict[str, int]:
    start = (now - timedelta(days=2)).date().isoformat()
    end = now.date().isoformat()
    resp = await api.get_user_stats_usage(panel_id, start, end)
    days = resp.get('categories') or []
    values = resp.get('sparklineData') or []
    return {str(d)[:10]: int(v or 0) for d, v in zip(days, values, strict=False)}


async def _fetch_accessible(api: RemnaWaveAPI, panel_id: int) -> list[dict]:
    # 404 тут не «нод нет», а «неизвестно»: пусть уйдёт в missing через исключение
    resp = (await api._make_request('GET', f'/api/users/{panel_id}/accessible-nodes', retry_transient=False)).get(
        'response'
    ) or {}
    return [{'uuid': n.get('uuid'), 'name': n.get('nodeName')} for n in resp.get('activeNodes') or []]


async def collect_snapshot(db: AsyncSession, api: RemnaWaveAPI, ticket: Ticket, now: datetime) -> Snapshot:
    """Собрать снимок. Не бросает исключений: сбои источников идут в ``missing``."""
    # Анкета лежит в самом тикете, а не в источниках: читаем её первой, пока ORM-объект не протух.
    diagnostics = getattr(ticket, 'diagnostics', None)
    snap = Snapshot(as_of=now, diagnostics=dict(diagnostics) if isinstance(diagnostics, dict) else None)
    missing = snap.missing

    bot = await _guard('bot_db', missing, lambda: _load_bot_data(db, ticket, now))
    if bot:
        for key, value in bot.items():
            setattr(snap, key, value)

    # Чтение БД закончено: транзакция не должна висеть, пока мы ждём панель.
    await end_transaction(db)

    remnawave_id = (snap.account or {}).get('remnawave_id')
    if remnawave_id is None:
        return snap
    try:
        panel_id = coerce_panel_user_id(remnawave_id)
    except Exception:
        return snap

    timeout = PANEL_SOURCE_TIMEOUT_SECONDS
    user, history, devices, usage, accessible, all_nodes = await asyncio.gather(
        _guard('panel_user', missing, lambda: _fetch_panel_user(api, panel_id), timeout),
        _guard('sub_requests', missing, lambda: _fetch_history(api, panel_id), timeout),
        _guard('devices', missing, lambda: _fetch_devices(api, panel_id), timeout),
        _guard('usage', missing, lambda: _fetch_usage(api, panel_id, now), timeout),
        _guard('squad_nodes', missing, lambda: _fetch_accessible(api, panel_id), timeout),
        _guard('nodes', missing, api.get_all_nodes, timeout),
    )
    missing.sort(key=lambda name: _SOURCE_ORDER.index(name) if name in _SOURCE_ORDER else len(_SOURCE_ORDER))

    if history is not None:
        snap.sub_requests_total, snap.sub_requests = history
    if devices is not None:
        snap.devices = devices
    if usage is not None:
        snap.usage_by_day = usage

    state = {n.uuid: n for n in all_nodes or []}
    if accessible is not None and all_nodes is not None:
        snap.squad_nodes = [
            {
                'name': n['name'],
                'is_connected': bool(state[n['uuid']].is_connected),
                'is_disabled': bool(state[n['uuid']].is_disabled),
            }
            for n in accessible
            if n['uuid'] in state
        ]
    if user is not None:
        last_uuid = user.pop('_last_node_uuid', None)
        node = state.get(last_uuid) if last_uuid else None
        user['last_node_name'] = node.name if node else None
        user['last_node_connected'] = bool(node.is_connected) if node else None
        user['last_node_disabled'] = bool(node.is_disabled) if node else None
        snap.panel_user = user
    return snap
