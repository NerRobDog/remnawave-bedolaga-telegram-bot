"""Детерминированные проверки по снимку клиента (порт спайка checks.py).

Чистая функция: никакого ввода-вывода, время берётся из ``Snapshot.as_of``.
Тексты флагов предназначены владельцу (по-русски) и в LLM-нагрузку не идут.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from app.services.support_assist.snapshot import Snapshot


Severity = Literal['high', 'mid', 'low', 'info']

_LIVE_STATUSES = {'active', 'trial'}
_MB = 1024 * 1024
_HISTORY_CAP = 24  # панель отдаёт не больше 24 последних запросов подписки


@dataclass(frozen=True)
class Flag:
    code: str
    sev: Severity
    text: str


def _hours_ago(now: datetime, value: datetime | None) -> float | None:
    if value is None:
        return None
    return round((now - value).total_seconds() / 3600, 1)


def _fmt(value: datetime | None) -> str:
    return value.strftime('%Y-%m-%d %H:%M') if value else '—'


def compute_flags(s: Snapshot) -> list[Flag]:
    now = s.as_of
    flags: list[Flag] = []

    def add(code: str, sev: Severity, text: str) -> None:
        flags.append(Flag(code, sev, text))

    sub = s.subscription
    pu = s.panel_user
    limit = (pu or {}).get('hwid_limit') or (sub or {}).get('device_limit')
    revokes = sorted(
        (a for a in s.cabinet_actions_7d if 'revoke' in str(a.get('button_id', '')).lower()),
        key=lambda a: a['at'],
        reverse=True,
    )
    reqs = sorted(s.sub_requests, key=lambda r: r['request_at'], reverse=True)

    # --- подписка в боте
    if not sub:
        add('no_subscription', 'high', 'У клиента нет подписки в боте')
    else:
        end = sub.get('end_date')
        status = sub.get('status')
        if status not in _LIVE_STATUSES or (end and end < now):
            add('sub_expired', 'high', f'Подписка {status}, срок до {_fmt(end)}')
        elif end and end - now < timedelta(days=1):
            add('sub_expiring', 'mid', f'Подписка истекает через {round((end - now).total_seconds() / 3600)} ч')
        if sub.get('is_daily_paused'):
            add('daily_paused', 'high', 'Посуточная подписка на паузе (не хватило баланса?)')

    # --- рассинхрон бота и панели
    if pu and sub:
        pe, be = pu.get('expire_at'), sub.get('end_date')
        if (
            str(pu.get('status', '')).upper() != 'ACTIVE'
            and sub.get('status') in _LIVE_STATUSES
            and not sub.get('is_daily_paused')
        ):
            add('desync_status', 'high', f'Бот: {sub.get("status")}, панель: {pu.get("status")} — рассинхрон')
        if pe and be and abs((pe - be).total_seconds()) > 6 * 3600:
            add('desync_expire', 'mid', f'Срок в боте {_fmt(be)} ≠ в панели {_fmt(pe)}')
        if sub.get('device_limit') and pu.get('hwid_limit') and sub['device_limit'] != pu['hwid_limit']:
            add('desync_devices', 'mid', f'Лимит устройств бот {sub["device_limit"]} ≠ панель {pu["hwid_limit"]}')

    # --- история запросов подписки
    total = s.sub_requests_total
    if total == 0:
        add('never_fetched', 'high', 'Приложение ни разу не забирало подписку — ссылка не добавлена в приложение')
    elif not reqs and (pu is not None or total is not None) and (total is None or total > _HISTORY_CAP):
        add(
            'sub_history_unavailable',
            'info',
            f'Панель хранит только {_HISTORY_CAP} последних запросов подписки; на момент обращения истории нет',
        )

    # --- подключение
    if pu and not pu.get('first_connected_at'):
        add('never_connected', 'high', 'Клиент ни разу не подключался к ноде')
    elif pu and pu.get('online_at'):
        off_h = _hours_ago(now, pu['online_at'])
        if off_h is not None and off_h > 24:
            add('offline_24h', 'mid', f'Последний онлайн {off_h} ч назад')

    # --- устройства
    if 'devices' not in s.missing:
        if limit and len(s.devices) >= limit:
            add('device_limit_full', 'high', f'Устройств {len(s.devices)} из {limit} — новое не подключится')
        new_dev = [d for d in s.devices if d.get('created_at') and 0 <= (_hours_ago(now, d['created_at']) or -1) < 24]
        if new_dev:
            add('new_device_24h', 'low', f'Новых устройств за сутки: {len(new_dev)}')

    # --- перевыпуск ключа
    if revokes:
        add(
            'revoked_recently',
            'high' if len(revokes) > 1 else 'mid',
            f'Перевыпуск ключа за 7 дней: {len(revokes)} раз, последний {_fmt(revokes[0]["at"])}',
        )
    last_revoke = (sub or {}).get('last_revoke_at')
    if last_revoke and reqs and reqs[0]['request_at'] < last_revoke:
        add('not_refetched_after_revoke', 'high', 'После перевыпуска приложение не забирало новую ссылку')

    # --- ноды (выключенные админом ноды не «лежат», а выведены из ротации)
    if pu and pu.get('last_node_name') and not pu.get('last_node_disabled') and not pu.get('last_node_connected', True):
        add('last_node_down', 'high', f'Нода последнего подключения {pu["last_node_name"]} недоступна')
    squad = [n for n in s.squad_nodes if not n.get('is_disabled')]
    up = [n for n in squad if n.get('is_connected')]
    if squad and not up:
        add('all_squad_nodes_down', 'high', 'Все ноды тарифа клиента недоступны')
    elif squad and len(up) < len(squad):
        down = ', '.join(n['name'] for n in squad if n not in up)
        add('some_squad_nodes_down', 'mid', f'Часть нод тарифа недоступна: {down}')

    # --- формат подписки
    # API истории запросов не отдаёт тип ответа (srr_response_type есть только в БД панели),
    # поэтому судим по User-Agent последнего запроса
    if reqs and str(reqs[0].get('user_agent') or '').lower().startswith('happ'):
        add('happ_base64', 'low', 'Клиент на Happ. Clash Mi даёт правила маршрутизации')

    # --- трафик: последние сутки неполные, судим по предыдущему полному дню
    if 'usage' not in s.missing:
        ref_day = (now.date() - timedelta(days=1)).isoformat()
        mb = round(s.usage_by_day.get(ref_day, 0) / _MB)
        if pu and pu.get('first_connected_at') and mb == 0:
            add('zero_traffic', 'mid', f'Трафика за {ref_day} нет')
        elif mb > 0:
            add(
                'traffic_flows',
                'info',
                f'Трафик идёт: {mb} МБ за {ref_day} — подключение есть, смотреть режим/сайты/белые списки',
            )

    # --- аккаунт, платежи, ошибки, массовый сбой
    if s.restrictions:
        add('restricted', 'high', 'Ограничения аккаунта: ' + '; '.join(s.restrictions))
    if s.bot_errors_24h:
        add('bot_errors', 'mid', f'Ошибок бота по клиенту за сутки: {s.bot_errors_24h}')
    if any(t.get('status') != 'completed' for t in s.transactions_48h):
        add('tx_pending', 'mid', 'Есть незавершённые платежи за 48 ч')
    if s.other_tickets_2h >= 2:
        add('mass_tickets', 'high', f'Ещё {s.other_tickets_2h} тикетов за 2 ч — возможен массовый сбой')

    return flags
