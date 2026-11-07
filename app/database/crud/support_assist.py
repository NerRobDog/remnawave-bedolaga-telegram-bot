"""CRUD карточек ассистента поддержки."""

from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import SupportAssistCard


logger = structlog.get_logger(__name__)


async def create_card(db: AsyncSession, **fields: Any) -> SupportAssistCard:
    card = SupportAssistCard(**fields)
    db.add(card)
    await db.commit()
    await db.refresh(card)
    return card


async def end_transaction(db: AsyncSession) -> None:
    """Закрыть открытую транзакцию и вернуть соединение в пул. Не бросает исключений.

    Вызывается перед долгим сетевым вызовом (панель, Claude, Telegram): иначе соединение
    простаивает «в транзакции» и PostgreSQL рвёт его по idle_in_transaction_session_timeout.
    Если commit не прошёл (сессия в сбойном состоянии), откатываем.
    """
    try:
        await db.commit()
    except Exception:
        logger.warning('support_assist.end_transaction_failed', exc_info=True)
        try:
            await db.rollback()
        except Exception:
            logger.warning('support_assist.rollback_failed', exc_info=True)


async def last_card_for_ticket(db: AsyncSession, ticket_id: int) -> SupportAssistCard | None:
    result = await db.execute(
        select(SupportAssistCard)
        .where(SupportAssistCard.ticket_id == ticket_id)
        .order_by(SupportAssistCard.id.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def get_card(db: AsyncSession, card_id: int) -> SupportAssistCard | None:
    result = await db.execute(select(SupportAssistCard).where(SupportAssistCard.id == card_id))
    return result.scalar_one_or_none()


async def claim_card_decision(
    db: AsyncSession,
    card_id: int,
    to_status: str,
    admin_id: int,
    from_status: str = 'shown',
    final_text: str | None = None,
) -> bool:
    """Атомарный переход статуса: True только у того, кто реально сменил статус.

    ``final_text`` — текст, который уходит клиенту; пишется тем же UPDATE, чтобы статус и текст не расходились.
    """
    values: dict[str, Any] = {'status': to_status, 'decided_by': admin_id, 'decided_at': datetime.now(UTC)}
    if final_text is not None:
        values['final_text'] = final_text
    result = await db.execute(
        update(SupportAssistCard)
        .where(SupportAssistCard.id == card_id, SupportAssistCard.status == from_status)
        .values(**values)
    )
    await db.commit()
    return result.rowcount == 1


async def release_card_decision(db: AsyncSession, card_id: int, from_status: str, admin_id: int) -> bool:
    """Вернуть карточку в 'shown', если отправка после claim не удалась.

    Откатывает только свой же claim (тот же статус и тот же админ): чужое решение не трогаем.
    """
    result = await db.execute(
        update(SupportAssistCard)
        .where(
            SupportAssistCard.id == card_id,
            SupportAssistCard.status == from_status,
            SupportAssistCard.decided_by == admin_id,
        )
        .values(status='shown', decided_by=None, decided_at=None, final_text=None)
    )
    await db.commit()
    return result.rowcount == 1


async def mark_auto_ask_sent(db: AsyncSession, card_id: int, question_ids: list[int]) -> bool:
    """Помечает карточку как отправившую автовопрос. False, если у тикета уже есть такая карточка."""
    try:
        await db.execute(
            update(SupportAssistCard)
            .where(SupportAssistCard.id == card_id)
            .values(auto_ask_question_ids=question_ids, auto_ask_sent_at=datetime.now(UTC))
        )
        await db.commit()
    except IntegrityError:
        await db.rollback()
        return False
    return True
