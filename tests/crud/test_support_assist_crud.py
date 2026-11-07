"""CRUD карточек support-assist: атомарные переходы статуса и один auto-ask на тикет."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import IntegrityError

from app.database.crud import support_assist as crud


def _db(rowcounts: list[int]) -> MagicMock:
    db = MagicMock()
    results = []
    for rc in rowcounts:
        r = MagicMock()
        r.rowcount = rc
        results.append(r)
    db.execute = AsyncMock(side_effect=results)
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    return db


@pytest.mark.asyncio
async def test_claim_card_decision_only_once():
    db = _db([1, 0])
    assert await crud.claim_card_decision(db, 5, to_status='sent', admin_id=10) is True
    assert await crud.claim_card_decision(db, 5, to_status='sent', admin_id=11) is False


@pytest.mark.asyncio
async def test_mark_auto_ask_sent_rejects_second_ticket_card():
    db = _db([1])
    assert await crud.mark_auto_ask_sent(db, 1, [1, 2]) is True

    db2 = MagicMock()
    db2.execute = AsyncMock(side_effect=IntegrityError('dup', {}, Exception('uq_support_assist_cards_auto_ask')))
    db2.commit = AsyncMock()
    db2.rollback = AsyncMock()
    assert await crud.mark_auto_ask_sent(db2, 2, [1]) is False
    db2.rollback.assert_awaited_once()


# --- реальные запросы на SQLite: решение владельца по карточке -------------------------------------------------------

from datetime import UTC, datetime

from sqlalchemy import select

from app.database.models import SupportAssistCard, Ticket, User
from tests.fixtures.sqlite_memory import memory_session


_TABLES = (User.__table__, Ticket.__table__, SupportAssistCard.__table__)


async def _seed_card(db, status='shown') -> int:
    db.add(User(id=1, telegram_id=1, language='ru'))
    db.add(Ticket(id=1, user_id=1, title='t', status='open'))
    db.add(
        SupportAssistCard(
            id=5, ticket_id=1, created_at=datetime.now(UTC), trigger='new_ticket', facts={}, flags=[], status=status
        )
    )
    await db.commit()
    db.expunge_all()
    return 5


async def _card(db) -> SupportAssistCard:
    db.expunge_all()
    return (await db.execute(select(SupportAssistCard).where(SupportAssistCard.id == 5))).scalar_one()


@pytest.mark.asyncio
async def test_get_card_returns_row_or_none(monkeypatch):
    async with memory_session(monkeypatch, _TABLES) as db:
        card_id = await _seed_card(db)

        assert (await crud.get_card(db, card_id)).ticket_id == 1
        assert await crud.get_card(db, 999) is None


@pytest.mark.asyncio
async def test_claim_stores_final_text_and_decider(monkeypatch):
    async with memory_session(monkeypatch, _TABLES) as db:
        card_id = await _seed_card(db)

        assert await crud.claim_card_decision(db, card_id, 'edited', 42, final_text='Ответ') is True
        assert await crud.claim_card_decision(db, card_id, 'sent', 43, final_text='Другой') is False

        row = await _card(db)
        assert (row.status, row.final_text, row.decided_by) == ('edited', 'Ответ', 42)
        assert row.decided_at is not None


@pytest.mark.asyncio
async def test_release_returns_own_claim_to_shown_and_clears_the_decision(monkeypatch):
    async with memory_session(monkeypatch, _TABLES) as db:
        card_id = await _seed_card(db)
        await crud.claim_card_decision(db, card_id, 'sent', 42, final_text='Ответ')

        assert await crud.release_card_decision(db, card_id, 'sent', 42) is True

        row = await _card(db)
        assert (row.status, row.final_text, row.decided_by, row.decided_at) == ('shown', None, None, None)
        # после отката карточку можно решить заново
        assert await crud.claim_card_decision(db, card_id, 'sent', 43) is True


@pytest.mark.asyncio
async def test_release_never_touches_someone_elses_decision(monkeypatch):
    async with memory_session(monkeypatch, _TABLES) as db:
        card_id = await _seed_card(db)
        await crud.claim_card_decision(db, card_id, 'sent', 43, final_text='Ответ')

        assert await crud.release_card_decision(db, card_id, 'sent', 42) is False

        row = await _card(db)
        assert (row.status, row.decided_by) == ('sent', 43)
