"""Решение по карточке ассистента поддержки: отправить ответ клиенту или пропустить.

Общий путь для кнопок карточки в Telegram (``app.handlers.admin.support_assist``) и для админского тикета
в кабинете (``app.cabinet.routes.admin_ticket_assist``): оба зовут эти функции, поэтому правила у них одни.

* Гонку решает ``claim_card_decision`` (один условный UPDATE): кабинет и Telegram, два админа, двойной
  тап не отправят клиенту больше одного сообщения.
* Отправка клиенту — ``send_admin_reply`` из ``tickets.py``, та же последовательность, что у ручного ответа.
  Если запись ответа упала после захвата, карточка возвращается в 'shown'.
* Кто решил, пишется в ``decided_by`` (BigInteger). Для админа из Telegram это его Telegram id; у админа
  кабинета без Telegram (вход по e-mail) — его id в нашей БД со знаком минус (``decider_id``): так он не
  совпадёт с настоящим Telegram id.
* Сообщение карточки в админском топике после решения правится на строку решения (``mark_card_message``);
  решение из кабинета подписано «из кабинета».

Функции не бросают исключений по ожидаемым причинам: исход описывает ``Verdict``, а вызывающий сам
решает, как его показать (всплывающее окно Telegram, код HTTP).
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from enum import StrEnum

import structlog
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.support_assist import (
    claim_card_decision,
    get_card,
    last_card_for_ticket,
    release_card_decision,
)
from app.database.crud.ticket import TicketCRUD
from app.database.models import Ticket, TicketStatus
from app.handlers.admin.tickets import send_admin_reply
from app.services.support_assist.card import Card
from app.services.support_assist.flags import Flag
from app.services.support_assist.render import TELEGRAM_LIMIT, render_card, render_card_checked
from app.utils.ticket_text import TICKET_MESSAGE_MAX_LENGTH


logger = structlog.get_logger(__name__)

DECISION_LINE = {
    'sent': '✅ отправлено',
    'edited': '✅ отправлено (текст изменён)',
    'dismissed': '⏭ пропущено',
}
CABINET_DECISION_LINE = {
    'sent': '✅ отправлено из кабинета',
    'edited': '✅ отправлено из кабинета (текст изменён)',
    'dismissed': '⏭ пропущено из кабинета',
}
# Карточка, которую ещё можно решить: 'shown' — с черновиком, 'fallback' — без модели (у неё только «Пропустить»).
SENDABLE_FROM = 'shown'
SKIPPABLE_FROM = ('shown', 'fallback')

_NO_KEYBOARD = InlineKeyboardMarkup(inline_keyboard=[])


class Verdict(StrEnum):
    OK = 'ok'
    ALREADY = 'already'
    NO_CARD = 'no_card'
    NO_TICKET = 'no_ticket'
    CLOSED = 'closed'
    NO_TEXT = 'no_text'
    COMMAND = 'command'
    TOO_LONG = 'too_long'
    DRAFT_NOT_SHOWN = 'draft_not_shown'
    NOT_LATEST = 'not_latest'
    FAILED = 'failed'


@dataclass(frozen=True, slots=True)
class CardView:
    """Простые значения карточки, снятые сразу после чтения.

    Откат сессии (неудачная запись ответа) протухает все загруженные ORM-строки, а чтение протухшего
    атрибута в async-сессии бросает MissingGreenlet; дальше работаем только с этими копиями.
    """

    id: int
    ticket_id: int
    status: str
    draft: str
    card: dict | None
    flags: list[dict]
    # Сообщение карточки в админском топике (None — не доставлено или не записано).
    admin_message_id: int | None = None


@dataclass(frozen=True, slots=True)
class Decision:
    """Исход решения. При ``OK`` ``status`` — итоговый статус карточки, ``body`` — текст, ушедший клиенту."""

    verdict: Verdict
    card: CardView | None = None
    status: str | None = None
    body: str | None = None


def decider_id(telegram_id: int | None, db_id: int) -> int:
    """Что писать в ``decided_by``: Telegram id админа, а без него — его id в нашей БД со знаком минус."""
    return telegram_id if telegram_id is not None else -db_id


def check_reply_text(text: str) -> Verdict | None:
    """Годится ли свой текст админа как ответ клиенту (None — годится).

    Команда с «/» в начале клиенту не уходит никогда: это опечатка в чате с ботом, а не ответ.
    """
    body = text.strip()
    if not body:
        return Verdict.NO_TEXT
    if body.startswith('/'):
        return Verdict.COMMAND
    if len(body) > TICKET_MESSAGE_MAX_LENGTH:
        return Verdict.TOO_LONG
    return None


async def load_card_view(db: AsyncSession, card_id: int) -> CardView | None:
    row = await get_card(db, card_id)
    if row is None:
        return None
    card = row.card if isinstance(row.card, dict) else None
    return CardView(
        id=row.id,
        ticket_id=row.ticket_id,
        status=row.status,
        draft=((card or {}).get('draft') or '').strip(),
        card=card,
        flags=list(row.flags or []),
        admin_message_id=row.admin_message_id,
    )


async def _is_latest(db: AsyncSession, view: CardView) -> bool:
    last = await last_card_for_ticket(db, view.ticket_id)
    return last is not None and last.id == view.id


def _card_text(view: CardView, line: str) -> str | None:
    """Карточка в том виде, как её отправил конвейер, плюс строка решения; в 4096 знаков не больше."""
    suffix = f'\n\n{line}'
    try:
        card = Card.model_validate(view.card) if view.card else None
        flags = [Flag(**f) for f in view.flags]
        return render_card(card, flags, view.ticket_id, limit=TELEGRAM_LIMIT - len(suffix)) + suffix
    except Exception:
        logger.warning('support_assist.card_text_rebuild_failed', card_id=view.id, exc_info=True)
        return None


def draft_fully_shown(view: CardView) -> bool:
    """Весь ли черновик был в сообщении карточки (карточка собирается так же, как её собрал конвейер).

    Не удалось собрать карточку заново — доказать нечем, считаем, что не весь.
    """
    try:
        card = Card.model_validate(view.card)
        flags = [Flag(**f) for f in view.flags]
        return not render_card_checked(card, flags, view.ticket_id).draft_truncated
    except Exception:
        logger.warning('support_assist.draft_visibility_check_failed', card_id=view.id, exc_info=True)
        return False


async def mark_card_message(
    bot: Bot,
    view: CardView,
    chat_id: int,
    message_id: int,
    status: str,
    *,
    from_cabinet: bool = False,
) -> None:
    """Снять кнопки с карточки и дописать, что решено. Сбой правки решения не отменяет."""
    line = (CABINET_DECISION_LINE if from_cabinet else DECISION_LINE)[status]
    text = _card_text(view, line)
    try:
        if text is not None:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                parse_mode='HTML',
                disable_web_page_preview=True,
                reply_markup=_NO_KEYBOARD,
            )
            return
    except TelegramAPIError:
        logger.warning('support_assist.card_edit_failed', card_id=view.id, chat_id=chat_id, exc_info=True)
    # Текст не собрался или не правится (сообщение старое, удалено): хотя бы снимаем кнопки.
    try:
        await bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=None)
    except TelegramAPIError:
        logger.warning('support_assist.card_keyboard_removal_failed', card_id=view.id, chat_id=chat_id, exc_info=True)


async def decide_and_send(
    bot: Bot,
    db: AsyncSession,
    *,
    card_id: int,
    decided_by: int,
    admin_db_id: int,
    text: str | None,
    to_status: str | None = None,
    ticket_id: int | None = None,
    require_latest: bool = False,
    require_draft_shown: bool = True,
) -> Decision:
    """Занять карточку и отправить клиенту ответ (``text`` или черновик карточки).

    ``to_status`` — итоговый статус ('sent' или 'edited'); без него: 'sent' для черновика и для текста,
    совпавшего с черновиком, иначе 'edited'.
    ``ticket_id`` — карточка обязана принадлежать этому тикету (иначе ``NO_CARD``).
    ``require_latest`` — решать можно только последнюю карточку тикета (иначе ``NOT_LATEST``).
    ``require_draft_shown`` — черновик уходит как есть, только если целиком лежал в карточке Telegram: владелец
    должен был его увидеть. Кабинет показывает черновик целиком сам и отключает проверку.
    """
    view = await load_card_view(db, card_id)
    if view is None or (ticket_id is not None and view.ticket_id != ticket_id):
        return Decision(Verdict.NO_CARD)
    if view.status != SENDABLE_FROM:
        return Decision(Verdict.ALREADY, view)
    if require_latest and not await _is_latest(db, view):
        return Decision(Verdict.NOT_LATEST, view)

    if text is None:
        body = view.draft
        problem = Verdict.NO_TEXT if not body else None
        if problem is None and len(body) > TICKET_MESSAGE_MAX_LENGTH:
            problem = Verdict.TOO_LONG
    else:
        body = text.strip()
        problem = check_reply_text(body)
    if problem is not None:
        return Decision(problem, view)
    # Черновик уходит клиенту как есть. Свой текст админ видел сам.
    if text is None and require_draft_shown and not draft_fully_shown(view):
        return Decision(Verdict.DRAFT_NOT_SHOWN, view)

    ticket = await TicketCRUD.get_ticket_by_id(db, view.ticket_id, load_messages=False, load_user=True)
    if ticket is None:
        return Decision(Verdict.NO_TICKET, view)
    # Ручной ответ в закрытый тикет админке недоступен, а add_message переоткрыл бы его как ANSWERED.
    if ticket.status == TicketStatus.CLOSED.value:
        return Decision(Verdict.CLOSED, view)

    status = to_status or ('sent' if text is None or body == view.draft else 'edited')
    # Занимаем карточку ДО ответа: проигравший второе нажатие ничего не отправляет.
    if not await claim_card_decision(db, card_id, status, decided_by, final_text=body):
        return Decision(Verdict.ALREADY, view)

    try:
        await send_admin_reply(bot, db, ticket, admin_db_id, body)
    except Exception:
        logger.exception('support_assist.reply_failed', card_id=card_id, ticket_id=view.ticket_id)
        # Сессия после сбоя может быть в сбойном состоянии: откат, затем возврат карточки в 'shown'.
        with contextlib.suppress(Exception):
            await db.rollback()
        try:
            await release_card_decision(db, card_id, status, decided_by)
        except Exception:
            logger.exception('support_assist.release_failed', card_id=card_id)
        return Decision(Verdict.FAILED, view)

    logger.info(
        'support_assist.decided', card_id=card_id, ticket_id=view.ticket_id, status=status, decided_by=decided_by
    )
    return Decision(Verdict.OK, view, status=status, body=body)


async def dismiss_card(
    db: AsyncSession,
    *,
    card_id: int,
    decided_by: int,
    ticket_id: int | None = None,
    require_latest: bool = False,
) -> Decision:
    """Пропустить карточку ('shown' или 'fallback' → 'dismissed'). Параметры — как у ``decide_and_send``."""
    view = await load_card_view(db, card_id)
    if view is None or (ticket_id is not None and view.ticket_id != ticket_id):
        return Decision(Verdict.NO_CARD)
    if view.status in SKIPPABLE_FROM and require_latest and not await _is_latest(db, view):
        return Decision(Verdict.NOT_LATEST, view)

    for from_status in SKIPPABLE_FROM:
        if await claim_card_decision(db, card_id, 'dismissed', decided_by, from_status=from_status):
            logger.info(
                'support_assist.decided',
                card_id=card_id,
                ticket_id=view.ticket_id,
                status='dismissed',
                decided_by=decided_by,
            )
            return Decision(Verdict.OK, view, status='dismissed')
    return Decision(Verdict.ALREADY, view)


async def notify_client_in_cabinet(db: AsyncSession, ticket: Ticket, body: str) -> None:
    """Уведомление клиенту в кабинете и push по WebSocket, как после ответа админа из кабинета. Не бросает."""
    try:
        from app.cabinet.routes.websocket import notify_user_ticket_reply
        from app.database.crud.ticket_notification import TicketNotificationCRUD

        notification = await TicketNotificationCRUD.create_user_notification_for_admin_reply(db, ticket, body)
        if notification:
            await notify_user_ticket_reply(ticket.user_id, ticket.id, body[:100])
    except Exception:
        logger.exception('support_assist.cabinet_notification_failed', ticket_id=getattr(ticket, 'id', None))
