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
