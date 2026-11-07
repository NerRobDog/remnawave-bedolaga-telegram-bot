"""Фоновый конвейер ассистента поддержки.

Запускается после того, как тикет сохранён и обычное уведомление админам ушло:
снимок и флаги, автовопрос клиенту (если включён), карточка Claude, отправка в топик
админов, запись в ``support_assist_cards``.

Любая ошибка логируется и глотается: тикеты, уведомления и SLA работают так же, как с
выключенным флагом.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import anthropic
import structlog
from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.support_assist import create_card, end_transaction, last_card_for_ticket, mark_auto_ask_sent
from app.database.crud.ticket import TicketCRUD
from app.database.database import AsyncSessionLocal
from app.database.models import SupportAssistCard, Ticket, TicketMessage
from app.external.remnawave_api import RemnaWaveAPI
from app.services.support_assist.card import CardResult, generate_card
from app.services.support_assist.flags import Flag, compute_flags
from app.services.support_assist.payload import build_payload
from app.services.support_assist.questions import is_bare, pick_questions, render_questions
from app.services.support_assist.render import build_card_keyboard, render_card
from app.services.support_assist.snapshot import Snapshot, collect_snapshot


logger = structlog.get_logger(__name__)

Trigger = Literal['new_ticket', 'new_message']

# Сколько последних сообщений клиента уходит в запрос к модели.
MAX_CLIENT_MESSAGES = 10
# Потолок на весь сбор снимка (страховка поверх таймаутов отдельных источников).
SNAPSHOT_TIMEOUT_SECONDS = 45.0
# Сколько разборов идёт одновременно в этом процессе: при сбое панели или Claude всплеск тикетов
# иначе превращается в десятки параллельных запросов наружу.
MAX_CONCURRENT_PIPELINES = 4

# Сильные ссылки на фоновые задачи: без них цикл событий может собрать задачу до конца.
_background_tasks: set[asyncio.Task[None]] = set()
# Тикеты, по которым конвейер уже идёт в этом процессе: вторая карточка параллельно не нужна.
_in_flight: set[int] = set()
_pipeline_slots = asyncio.Semaphore(MAX_CONCURRENT_PIPELINES)


def notification_bot() -> Bot | None:
    """Экземпляр бота, через который уже уходят админ-уведомления (для кабинетных роутов)."""
    try:
        from app.services.maintenance_service import maintenance_service

        return maintenance_service._bot or None
    except Exception:
        logger.exception('support_assist.bot_lookup_failed')
        return None


def schedule_assist(bot: Bot | None, ticket_id: int, trigger: Trigger) -> None:
    """Поставить разбор тикета в фон. Не бросает исключений и ничего не ждёт."""
    if not settings.SUPPORT_ASSIST_ENABLED:
        return
    if bot is None:
        logger.warning('support_assist.no_bot', ticket_id=ticket_id, trigger=trigger)
        return
    coro = _run_safe(bot, ticket_id, trigger)
    try:
        task = asyncio.create_task(coro)
    except Exception:
        coro.close()
        logger.exception('support_assist.schedule_failed', ticket_id=ticket_id, trigger=trigger)
        return
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def _run_safe(bot: Bot, ticket_id: int, trigger: Trigger) -> None:
    """Открыть свою сессию БД, клиент панели и клиент Claude; поймать всё."""
    try:
        from app.services.remnawave_service import remnawave_service

        # Ждём очередь до открытия сессии БД: ожидающий разбор не должен занимать соединение.
        async with _pipeline_slots, AsyncSessionLocal() as db, remnawave_service.get_api_client() as api:
            if settings.ANTHROPIC_API_KEY:
                async with anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY) as client:
                    await run_assist(db, bot, api, client, ticket_id, trigger, datetime.now(UTC))
            else:
                await run_assist(db, bot, api, None, ticket_id, trigger, datetime.now(UTC))
    except Exception:
        logger.exception('support_assist.pipeline_failed', ticket_id=ticket_id, trigger=trigger)


async def run_assist(
    db: AsyncSession,
    bot: Bot,
    api: RemnaWaveAPI,
    client: anthropic.AsyncAnthropic | None,
    ticket_id: int,
    trigger: Trigger,
    now: datetime,
) -> SupportAssistCard | None:
    """Один проход конвейера. ``None`` — делать нечего (троттлинг, нет тикета, нет админ-чата)."""
    if ticket_id in _in_flight:
        logger.info('support_assist.already_running', ticket_id=ticket_id)
        return None
    _in_flight.add(ticket_id)
    try:
        return await _run(db, bot, api, client, ticket_id, trigger, now)
    finally:
        _in_flight.discard(ticket_id)


async def _run(
    db: AsyncSession,
    bot: Bot,
    api: RemnaWaveAPI,
    client: anthropic.AsyncAnthropic | None,
    ticket_id: int,
    trigger: Trigger,
    now: datetime,
) -> SupportAssistCard | None:
    # Карточке некуда уходить, если админ-уведомления выключены: не тратим деньги впустую.
    chat_id = settings.get_admin_notifications_chat_id()
    if not settings.is_admin_notifications_enabled() or chat_id is None:
        return None

    # 1. Троттлинг: не чаще раза в RECOMPUTE минут на тикет.
    last = await last_card_for_ticket(db, ticket_id)
    window = timedelta(minutes=settings.SUPPORT_ASSIST_RECOMPUTE_MINUTES)
    if last is not None and last.created_at is not None and now - last.created_at < window:
        logger.info('support_assist.throttled', ticket_id=ticket_id, trigger=trigger)
        return None

    ticket = await TicketCRUD.get_ticket_by_id(db, ticket_id, load_messages=True, load_user=True)
    if ticket is None:
        return None
    client_messages = sorted((m for m in ticket.messages if not m.is_from_admin), key=lambda m: m.id)

    # 2. Снимок и флаги. collect_snapshot сам закрывает транзакцию перед запросами к панели; при
    # сбое или обрыве по таймауту сессия может остаться в сбойном состоянии, приводим её в порядок.
    try:
        async with asyncio.timeout(SNAPSHOT_TIMEOUT_SECONDS):
            snapshot = await collect_snapshot(db, api, ticket, now)
    except Exception:
        logger.exception('support_assist.snapshot_failed', ticket_id=ticket_id)
        await end_transaction(db)
        snapshot = Snapshot(as_of=now, missing=['snapshot'])
    flags = compute_flags(snapshot)
    payload = build_payload(snapshot, flags)

    # Строка карточки появляется сразу: на неё опирается атомарная отметка автовопроса, и по ней
    # троттлинг видит разбор, который ещё генерируется. Статус 'failed' — пока ничего не доставлено.
    row = await create_card(
        db,
        ticket_id=ticket_id,
        created_at=now,
        trigger=trigger,
        facts=payload,
        flags=[asdict(f) for f in flags],
        status='failed',
    )
    # id берём сейчас: после отката в mark_auto_ask_sent или end_transaction объект протухает,
    # а обращение к протухшему атрибуту в async-сессии бросает MissingGreenlet.
    card_id = row.id
    # commit() + refresh() в create_card оставляют транзакцию открытой, а дальше идут сеть и Claude.
    await end_transaction(db)

    # 3. Автовопрос клиенту (только на новый тикет, один раз).
    if trigger == 'new_ticket' and settings.SUPPORT_ASSIST_AUTO_ASK_ENABLED and client_messages:
        try:
            await _auto_ask(db, bot, ticket, row, client_messages[0], snapshot, flags)
        except Exception:
            logger.exception('support_assist.auto_ask_failed', ticket_id=ticket_id)
        # Уведомление клиенту читает БД (последнее сообщение, вебхуки): закрываем транзакцию до Claude.
        await end_transaction(db)

    # 4. Карточка от модели (или без неё).
    if client is None:
        result = CardResult(card=None, error='no_api_key')
    else:
        result = await generate_card(client, payload, [_client_text(m) for m in client_messages[-MAX_CLIENT_MESSAGES:]])
    if result.error:
        logger.warning('support_assist.card_without_model', ticket_id=ticket_id, error=result.error)

    # 5. Рендер и отправка владельцу отдельным сообщением в топик тикетов.
    show_send = settings.SUPPORT_ASSIST_SEND_BUTTONS_ENABLED and result.card is not None
    message_id = await _send_card(
        bot,
        chat_id,
        render_card(result.card, flags, ticket_id),
        build_card_keyboard(card_id, show_send),
    )

    # 6. Запись результата: токены и цена сохраняются, даже если отправка не удалась.
    row.card = result.card.model_dump(mode='json') if result.card else None
    row.model = result.model
    row.input_tokens = result.input_tokens
    row.output_tokens = result.output_tokens
    row.cache_read_tokens = result.cache_read_tokens
    row.cost_usd = result.cost_usd
    row.admin_message_id = message_id
    if message_id is None:
        row.status = 'failed'
    else:
        row.status = 'shown' if result.card is not None else 'fallback'
    await db.commit()
    return row


def _client_text(message: Any) -> str:
    text = (message.message_text or '').strip()
    if text:
        return text
    return '[вложение без текста]' if message.has_media else ''


async def _auto_ask(
    db: AsyncSession,
    bot: Bot,
    ticket: Ticket,
    row: SupportAssistCard,
    first: Any,
    snapshot: Snapshot,
    flags: list[Flag],
) -> None:
    text = first.message_text or ''
    has_media = bool(first.has_media)
    if not is_bare(text, has_media):
        return
    question_ids = pick_questions(text, has_media, snapshot, flags)
    if not question_ids:
        return
    # Сначала занимаем «право спросить»: уникальный индекс не даёт спросить дважды.
    if not await mark_auto_ask_sent(db, row.id, question_ids):
        logger.info('support_assist.auto_ask_already_sent', ticket_id=ticket.id)
        return

    lang = (snapshot.account or {}).get('language') or getattr(ticket.user, 'language', None)
    body = render_questions(question_ids, lang or settings.DEFAULT_LANGUAGE)
    # Сообщение системы, а не владельца. Статус и updated_at тикета не трогаем: он остаётся OPEN,
    # и SLA-напоминание владельцу сработает в обычный срок.
    message = TicketMessage(
        ticket_id=ticket.id,
        user_id=ticket.user_id,
        message_text=body,
        is_from_admin=True,
        is_system=True,
        has_media=False,
    )
    db.add(message)
    await db.commit()

    # Доставка клиенту: те же три шага, что у ручного ответа из кабинета (reply_to_ticket в
    # admin_tickets.py), плюс Telegram или e-mail из бота. Тикет с кабинета или из мобильного
    # приложения иначе увидел бы вопросы только после перезагрузки. Каждый шаг изолирован.
    await _emit_message_added(db, ticket, message, body)

    try:
        from app.handlers.admin.tickets import notify_user_about_ticket_reply

        await notify_user_about_ticket_reply(bot, ticket, body, db)
    except Exception:
        logger.exception('support_assist.auto_ask_telegram_failed', ticket_id=ticket.id)

    await _notify_in_cabinet(db, ticket, body)


async def _emit_message_added(db: AsyncSession, ticket: Ticket, message: TicketMessage, body: str) -> None:
    """Событие для моста мобильного сокета поддержки; сообщение уже сохранено."""
    try:
        from app.services.event_emitter import event_emitter

        await event_emitter.emit(
            'ticket.message_added',
            {
                'ticket_id': ticket.id,
                'message_id': message.id,
                'user_id': ticket.user_id,
                'is_from_admin': True,
                'message_text': body[:200],
                'has_media': False,
                'status': ticket.status,
            },
            db=db,
        )
    except Exception:
        logger.exception('support_assist.auto_ask_event_failed', ticket_id=ticket.id)


async def _notify_in_cabinet(db: AsyncSession, ticket: Ticket, body: str) -> None:
    """Уведомление в кабинете и push по WebSocket, как после ответа админа из кабинета."""
    try:
        from app.cabinet.routes.websocket import notify_user_ticket_reply
        from app.database.crud.ticket_notification import TicketNotificationCRUD

        notification = await TicketNotificationCRUD.create_user_notification_for_admin_reply(db, ticket, body)
        if notification:
            await notify_user_ticket_reply(ticket.user_id, ticket.id, body[:100])
    except Exception:
        logger.exception('support_assist.auto_ask_cabinet_failed', ticket_id=ticket.id)


async def _send_card(bot: Bot, chat_id: int, text: str, keyboard: Any) -> int | None:
    kwargs: dict[str, Any] = {
        'chat_id': chat_id,
        'text': text,
        'parse_mode': 'HTML',
        'disable_web_page_preview': True,
        'reply_markup': keyboard,
    }
    topic_id = settings.ADMIN_NOTIFICATIONS_TICKET_TOPIC_ID
    if topic_id:
        kwargs['message_thread_id'] = topic_id
    try:
        sent = await bot.send_message(**kwargs)
    except Exception:
        logger.exception('support_assist.card_send_failed', chat_id=chat_id)
        return None
    return sent.message_id
