"""Фоновый конвейер ассистента поддержки.

Запускается после того, как тикет сохранён и обычное уведомление админам ушло:
снимок и флаги, автовопрос клиенту (если включён), карточка Claude, отправка в топик
админов, запись в ``support_assist_cards``.

Любая ошибка логируется и глотается: тикеты, уведомления и SLA работают так же, как с
выключенным флагом.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
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
from app.services.support_assist.render import TRUNCATED_DRAFT_NOTICE, build_card_keyboard, render_card_checked
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
# Потолок на весь вызов Claude сверх таймаута одного HTTP-запроса (SUPPORT_ASSIST_TIMEOUT_SECONDS): таймаут SDK
# действует на каждую попытку отдельно, и без общего потолка карточка держала бы слот конвейера минутами.
CARD_TIMEOUT_MARGIN_SECONDS = 10.0
# Одна повторная попытка на ошибки сети и 429/5xx; по умолчанию SDK повторяет дважды.
CLAUDE_MAX_RETRIES = 1

# Сильные ссылки на фоновые задачи: без них цикл событий может собрать задачу до конца.
_background_tasks: set[asyncio.Task[None]] = set()
# Тикеты, по которым конвейер уже идёт в этом процессе: вторая карточка параллельно не нужна.
_in_flight: set[int] = set()
_pipeline_slots = asyncio.Semaphore(MAX_CONCURRENT_PIPELINES)


@dataclass(frozen=True, slots=True)
class _TicketView:
    """Простые значения тикета, снятые сразу после загрузки.

    Любой откат сессии (``end_transaction`` после сбойного commit, IntegrityError в
    ``mark_auto_ask_sent``) протухает все загруженные в неё ORM-строки, а чтение протухшего
    атрибута в async-сессии бросает MissingGreenlet: ``expire_on_commit=False`` защищает только
    от commit. Поэтому дальше конвейер читает только эти копии, а не ORM-объекты.
    """

    language: str | None
    # None — у тикета нет сообщений клиента.
    first_text: str | None
    first_has_media: bool
    client_texts: list[str]


def _view_of(ticket: Ticket) -> _TicketView:
    client_messages = sorted((m for m in ticket.messages if not m.is_from_admin), key=lambda m: m.id)
    first = client_messages[0] if client_messages else None
    return _TicketView(
        language=getattr(ticket.user, 'language', None),
        first_text=(first.message_text or '') if first is not None else None,
        first_has_media=bool(first.has_media) if first is not None else False,
        client_texts=[_client_text(m) for m in client_messages[-MAX_CLIENT_MESSAGES:]],
    )


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
                async with anthropic.AsyncAnthropic(
                    api_key=settings.ANTHROPIC_API_KEY, max_retries=CLAUDE_MAX_RETRIES
                ) as client:
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
    # Карточке некуда уходить, если админ-уведомления выключены: модель не зовём и в топик не шлём.
    # Автовопрос клиенту от этого не зависит: у него свой флаг, и он идёт и без админ-чата.
    chat_id = settings.get_admin_notifications_chat_id()
    notify_admins = settings.is_admin_notifications_enabled() and chat_id is not None
    wants_auto_ask = trigger == 'new_ticket' and settings.SUPPORT_ASSIST_AUTO_ASK_ENABLED
    if not notify_admins and not wants_auto_ask:
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
    # Копии простых значений: после любого отката сессии ORM-объекты тикета читать нельзя.
    view = _view_of(ticket)

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
    if wants_auto_ask and view.first_text is not None:
        try:
            await _auto_ask(db, bot, ticket_id, card_id, view, snapshot, flags)
        except Exception:
            logger.exception('support_assist.auto_ask_failed', ticket_id=ticket_id)
        # Уведомление клиенту читает БД (последнее сообщение, вебхуки): закрываем транзакцию до Claude.
        await end_transaction(db)

    if not notify_admins or chat_id is None:
        # Только автовопрос: строку оставляем (на ней держится отметка «спросили»), но не как сбой.
        row.status = 'skipped'
        await db.commit()
        return row

    # 4. Карточка от модели (или без неё).
    if client is None:
        result = CardResult(card=None, error='no_api_key')
    else:
        result = await _generate_capped(client, payload, view.client_texts)
    if result.error:
        logger.warning('support_assist.card_without_model', ticket_id=ticket_id, error=result.error)

    # 5. Рендер и отправка владельцу отдельным сообщением в топик тикетов.
    show_send = settings.SUPPORT_ASSIST_SEND_BUTTONS_ENABLED and result.card is not None
    # «Отправить» уходит клиенту черновик как есть: если он не влез в карточку, владелец его целиком не видел.
    rendered = render_card_checked(
        result.card,
        flags,
        ticket_id,
        truncation_notice=TRUNCATED_DRAFT_NOTICE if show_send else None,
    )
    message_id = await _send_card(
        bot,
        chat_id,
        rendered.text,
        build_card_keyboard(card_id, show_send, draft_truncated=rendered.draft_truncated),
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


def card_deadline_seconds() -> float:
    """Сколько всего можно ждать карточку от Claude: таймаут запроса плюс запас на разбор ответа."""
    return float(settings.SUPPORT_ASSIST_TIMEOUT_SECONDS) + CARD_TIMEOUT_MARGIN_SECONDS


async def _generate_capped(client: anthropic.AsyncAnthropic, payload: dict, client_texts: list[str]) -> CardResult:
    """``generate_card`` под общим потолком; по таймауту — результат без карточки (рисуется карточка без модели)."""
    try:
        async with asyncio.timeout(card_deadline_seconds()):
            return await generate_card(client, payload, client_texts)
    except TimeoutError:
        logger.warning('support_assist.card_timeout', deadline_seconds=card_deadline_seconds())
        return CardResult(card=None, error='timeout')


def _client_text(message: Any) -> str:
    text = (message.message_text or '').strip()
    if text:
        return text
    return '[вложение без текста]' if message.has_media else ''


async def _auto_ask(
    db: AsyncSession,
    bot: Bot,
    ticket_id: int,
    card_id: int,
    view: _TicketView,
    snapshot: Snapshot,
    flags: list[Flag],
) -> None:
    text = view.first_text or ''
    has_media = view.first_has_media
    if not is_bare(text, has_media):
        return
    question_ids = pick_questions(text, has_media, snapshot, flags)
    if not question_ids:
        return
    # К этому месту сессия могла откатиться (сбой сбора, end_transaction), и тикет из _run протух.
    # Чтение свежее; ORM-тикет нужен только отправке (уведомления берут из него id, пользователя, статус).
    ticket = await TicketCRUD.get_ticket_by_id(db, ticket_id, load_messages=False, load_user=True)
    if ticket is None:
        return
    # Сначала занимаем «право спросить»: уникальный индекс не даёт спросить дважды. Проигрыш откатывает
    # сессию и протухает ticket, поэтому после него в этой ветке только ticket_id.
    if not await mark_auto_ask_sent(db, card_id, question_ids):
        logger.info('support_assist.auto_ask_already_sent', ticket_id=ticket_id)
        return

    lang = (snapshot.account or {}).get('language') or view.language
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
