"""Карточка ассистента поддержки в админском тикете кабинета.

Та же карточка, что ассистент шлёт в топик тикетов админского чата Telegram, и те же действия:
``GET`` отдаёт последнюю карточку тикета и ответы его анкеты, ``send`` отправляет клиенту черновик или
исправленный текст, ``skip`` пропускает карточку. Решение принимает ``app.services.support_assist.decision``,
общий с кнопками Telegram: гонку кабинета и Telegram решает один условный UPDATE, клиент получает одно сообщение.

Сообщение карточки в Telegram после решения из кабинета правится на строку «… из кабинета».
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any, Literal

import structlog
from aiogram import Bot
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.support_assist import last_card_for_ticket
from app.database.crud.ticket import TicketCRUD
from app.database.models import SupportAssistCard, User
from app.services.support_assist.card import Card
from app.services.support_assist.decision import (
    SKIPPABLE_FROM,
    CardView,
    Decision,
    Verdict,
    decide_and_send,
    decider_id,
    dismiss_card,
    mark_card_message,
    notify_client_in_cabinet,
)
from app.services.support_assist.flags import Flag
from app.services.support_assist.form_options import get_form_options
from app.services.support_assist.pipeline import notification_bot
from app.services.support_assist.render import flag_facts
from app.services.support_assist.ticket_form import diagnostics_rows
from app.utils.ticket_text import TICKET_MESSAGE_MAX_LENGTH

from ..dependencies import get_cabinet_db, require_permission


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/admin/tickets', tags=['Cabinet Admin Ticket Assist'])

# Ответы анкеты показываются владельцу по-русски, как в уведомлении Telegram.
DIAGNOSTICS_LANGUAGE = 'ru'


class AssistFact(BaseModel):
    icon: str
    text: str


class AssistCardResponse(BaseModel):
    id: int
    created_at: datetime
    status: str
    # Карточку больше нельзя решить (отправлена, пропущена, занята или так и не доставлена).
    decided: bool
    # Разбор без модели: факты построены из флагов, черновика нет.
    no_model: bool
    facts: list[AssistFact]
    cause: str
    owner_action: str
    ask_client: list[str]
    draft: str


class AssistDiagnostic(BaseModel):
    label: str
    value: str


class AssistResponse(BaseModel):
    card: AssistCardResponse | None
    can_send: bool
    diagnostics: list[AssistDiagnostic] | None


class AssistSendRequest(BaseModel):
    """Без ``text`` уходит черновик карточки."""

    text: str | None = None


class AssistDecisionResponse(BaseModel):
    status: Literal['sent', 'skipped']


# Исход решения → (код HTTP, пояснение).
_REFUSALS: dict[Verdict, tuple[int, str]] = {
    Verdict.NO_CARD: (status.HTTP_404_NOT_FOUND, 'Card not found'),
    Verdict.NO_TICKET: (status.HTTP_404_NOT_FOUND, 'Ticket not found'),
    Verdict.ALREADY: (status.HTTP_409_CONFLICT, 'Card is already decided'),
    Verdict.NOT_LATEST: (status.HTTP_409_CONFLICT, 'Card is not the latest for this ticket'),
    Verdict.CLOSED: (status.HTTP_409_CONFLICT, 'Ticket is closed'),
    Verdict.NO_TEXT: (status.HTTP_422_UNPROCESSABLE_ENTITY, 'Reply text is empty'),
    Verdict.COMMAND: (status.HTTP_422_UNPROCESSABLE_ENTITY, 'Reply text must not start with "/"'),
    Verdict.TOO_LONG: (
        status.HTTP_422_UNPROCESSABLE_ENTITY,
        f'Reply text is longer than {TICKET_MESSAGE_MAX_LENGTH} characters',
    ),
    Verdict.DRAFT_NOT_SHOWN: (status.HTTP_422_UNPROCESSABLE_ENTITY, 'Draft was not shown in full'),
    Verdict.FAILED: (status.HTTP_500_INTERNAL_SERVER_ERROR, 'Failed to send the reply'),
}


def _raise_unless_ok(decision: Decision) -> None:
    if decision.verdict is Verdict.OK:
        return
    code, detail = _REFUSALS[decision.verdict]
    raise HTTPException(status_code=code, detail=detail)


def _model_card(row: SupportAssistCard) -> Card | None:
    """Карточка модели; нет её (разбор без модели) или она повреждена — ``None``."""
    if not isinstance(row.card, dict):
        return None
    try:
        return Card.model_validate(row.card)
    except Exception:
        logger.warning('support_assist.cabinet_card_invalid', card_id=row.id, exc_info=True)
        return None


def _flags(row: SupportAssistCard) -> list[Flag]:
    try:
        return [Flag(**f) for f in (row.flags or [])]
    except Exception:
        logger.warning('support_assist.cabinet_flags_invalid', card_id=row.id, exc_info=True)
        return []


def _card_response(row: SupportAssistCard) -> AssistCardResponse:
    model_card = _model_card(row)
    if model_card is None:
        # Как рисует Telegram: по факту на каждый флаг, остальных разделов нет.
        facts = [AssistFact(icon=icon, text=text) for icon, text in flag_facts(_flags(row))]
        cause = owner_action = draft = ''
        ask_client: list[str] = []
    else:
        facts = [AssistFact(icon=f.icon, text=f.text) for f in model_card.facts]
        cause, owner_action = model_card.cause, model_card.owner_action
        ask_client = list(model_card.ask_client)
        draft = (model_card.draft or '').strip()
    return AssistCardResponse(
        id=row.id,
        created_at=row.created_at,
        status=row.status,
        decided=row.status not in SKIPPABLE_FROM,
        no_model=model_card is None,
        facts=facts,
        cause=cause,
        owner_action=owner_action,
        ask_client=ask_client,
        draft=draft,
    )


@contextlib.asynccontextmanager
async def _telegram_bot() -> AsyncIterator[Bot]:
    """Бот, через который уже уходят админ-уведомления; нет его в процессе — свой, на время запроса."""
    shared = notification_bot()
    if shared is not None:
        yield shared
        return
    from app.bot_factory import create_bot

    async with create_bot() as own:
        yield own


async def _mark_telegram_card(view: CardView | None, decision_status: str, bot: Bot | None = None) -> None:
    """Дописать в сообщение карточки в админском топике, что решено из кабинета. Решение это не отменяет."""
    chat_id = settings.get_admin_notifications_chat_id()
    if view is None or chat_id is None or view.admin_message_id is None:
        return
    try:
        if bot is not None:
            await mark_card_message(bot, view, chat_id, view.admin_message_id, decision_status, from_cabinet=True)
            return
        async with _telegram_bot() as own:
            await mark_card_message(own, view, chat_id, view.admin_message_id, decision_status, from_cabinet=True)
    except Exception:
        logger.warning('support_assist.cabinet_card_mark_failed', card_id=view.id, exc_info=True)


@router.get('/{ticket_id}/assist', response_model=AssistResponse)
async def get_ticket_assist(
    ticket_id: int,
    admin: User = Depends(require_permission('tickets:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Последняя карточка ассистента по тикету и ответы анкеты клиента."""
    ticket = await TicketCRUD.get_ticket_by_id(db, ticket_id, load_messages=False)
    if ticket is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Ticket not found')

    raw_answers: Any = ticket.diagnostics
    rows = diagnostics_rows(raw_answers, DIAGNOSTICS_LANGUAGE, await get_form_options())
    diagnostics = [AssistDiagnostic(label=label, value=value) for label, value in rows] or None

    row = await last_card_for_ticket(db, ticket_id)
    card = _card_response(row) if row is not None else None
    can_send = bool(
        settings.SUPPORT_ASSIST_SEND_BUTTONS_ENABLED and card is not None and not card.decided and card.draft
    )
    return AssistResponse(card=card, can_send=can_send, diagnostics=diagnostics)


@router.post('/{ticket_id}/assist/{card_id}/send', response_model=AssistDecisionResponse)
async def send_ticket_assist(
    ticket_id: int,
    card_id: int,
    request: AssistSendRequest | None = None,
    admin: User = Depends(require_permission('tickets:reply')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Отправить клиенту черновик карточки или исправленный текст (как «Отправить» и «Править» в Telegram)."""
    if not settings.SUPPORT_ASSIST_SEND_BUTTONS_ENABLED:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail='Sending from the card is disabled')

    # Идентичность снимаем сразу: после отката сессии (сбой отправки) ORM-админ протухает.
    admin_db_id = admin.id
    decided_by = decider_id(admin.telegram_id, admin_db_id)
    async with _telegram_bot() as bot:
        decision = await decide_and_send(
            bot,
            db,
            card_id=card_id,
            decided_by=decided_by,
            admin_db_id=admin_db_id,
            text=request.text if request is not None else None,
            ticket_id=ticket_id,
            require_latest=True,
            # Кабинет показывает черновик целиком, поэтому он уходит как есть, даже если в карточку Telegram не влез.
            require_draft_shown=False,
        )
        _raise_unless_ok(decision)

        # Дальше всё необязательное: клиент уже получил ответ.
        try:
            ticket = await TicketCRUD.get_ticket_by_id(db, ticket_id, load_messages=False)
            if ticket is not None and decision.body:
                await notify_client_in_cabinet(db, ticket, decision.body)
        except Exception:
            logger.warning('support_assist.cabinet_client_notice_failed', ticket_id=ticket_id, exc_info=True)
        await _mark_telegram_card(decision.card, decision.status or 'sent', bot)
    return AssistDecisionResponse(status='sent')


@router.post('/{ticket_id}/assist/{card_id}/skip', response_model=AssistDecisionResponse)
async def skip_ticket_assist(
    ticket_id: int,
    card_id: int,
    admin: User = Depends(require_permission('tickets:reply')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Пропустить карточку (как «Пропустить» в Telegram)."""
    decision = await dismiss_card(
        db,
        card_id=card_id,
        decided_by=decider_id(admin.telegram_id, admin.id),
        ticket_id=ticket_id,
        require_latest=True,
    )
    _raise_unless_ok(decision)

    await _mark_telegram_card(decision.card, 'dismissed')
    return AssistDecisionResponse(status='skipped')
