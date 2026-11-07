"""Конвейер на настоящих ORM-строках и SQLite.

Любой откат сессии протухает все загруженные в неё ORM-объекты (``expire_on_commit=False``
защищает только от commit), а обращение к протухшему атрибуту в async-сессии бросает
MissingGreenlet. Подменные тикеты из ``SimpleNamespace`` этого не воспроизводят, поэтому
откаты проверяются здесь.
"""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select

from app.config import settings
from app.database.models import SupportAssistCard, Ticket, TicketMessage, User
from app.services.support_assist import pipeline
from tests.fixtures.sqlite_memory import memory_session
from tests.services.support_assist.fixtures import AS_OF, healthy


NOW = AS_OF
_TABLES = (User.__table__, Ticket.__table__, TicketMessage.__table__, SupportAssistCard.__table__)
CLIENT_TEXT = 'не работает'
VALID = {
    'facts': [{'icon': '✅', 'text': 'Подписка активна'}],
    'cause': 'Нужны детали',
    'owner_action': 'нет',
    'ask_client': ['Какое приложение?'],
    'draft': 'Уточните, пожалуйста, устройство.',
}


@pytest.fixture(autouse=True)
def cabinet(monkeypatch):
    """Снаружи конвейера остаются только панель, Claude, Telegram и кабинетные побочные эффекты."""
    for name, value in {
        'SUPPORT_ASSIST_ENABLED': True,
        'SUPPORT_ASSIST_AUTO_ASK_ENABLED': False,
        'SUPPORT_ASSIST_SEND_BUTTONS_ENABLED': False,
        'SUPPORT_ASSIST_RECOMPUTE_MINUTES': 10,
        'ADMIN_NOTIFICATIONS_ENABLED': True,
        'ADMIN_NOTIFICATIONS_CHAT_ID': '-100500',
        'ADMIN_NOTIFICATIONS_TICKET_TOPIC_ID': 77,
    }.items():
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(pipeline, 'collect_snapshot', AsyncMock(return_value=healthy()))
    emit = AsyncMock()
    notification = AsyncMock(return_value=SimpleNamespace(id=1))
    push = AsyncMock()
    monkeypatch.setattr('app.services.event_emitter.event_emitter.emit', emit)
    monkeypatch.setattr(
        'app.database.crud.ticket_notification.TicketNotificationCRUD.create_user_notification_for_admin_reply',
        notification,
    )
    monkeypatch.setattr('app.cabinet.routes.websocket.notify_user_ticket_reply', push)
    return SimpleNamespace(emit=emit, notification=notification, push=push)


@pytest.fixture
def notify(monkeypatch):
    mock = AsyncMock()
    monkeypatch.setattr('app.handlers.admin.tickets.notify_user_about_ticket_reply', mock)
    return mock


@pytest.fixture
def bot():
    mock = MagicMock()
    mock.send_message = AsyncMock(return_value=SimpleNamespace(message_id=555))
    return mock


def _client():
    usage = SimpleNamespace(
        input_tokens=1000, output_tokens=500, cache_read_input_tokens=2000, cache_creation_input_tokens=0
    )
    response = SimpleNamespace(
        content=[SimpleNamespace(type='text', text=json.dumps(VALID))],
        usage=usage,
        stop_reason='end_turn',
        model='claude-opus-5-5',
    )
    client = MagicMock()
    client.beta.messages.create = AsyncMock(return_value=response)
    return client


async def _seed(db) -> int:
    """Тикет с одним сообщением клиента; сессия после этого чистая, как у фонового разбора."""
    user = User(telegram_id=1, username='u', first_name='U', language='ru')
    db.add(user)
    await db.flush()
    ticket = Ticket(user_id=user.id, title='t', status='open', updated_at=NOW - timedelta(hours=2))
    db.add(ticket)
    await db.flush()
    db.add(
        TicketMessage(
            ticket_id=ticket.id, user_id=user.id, message_text=CLIENT_TEXT, is_from_admin=False, has_media=False
        )
    )
    await db.commit()
    db.expunge_all()
    return ticket.id


async def _run(db, bot, ticket_id, *, now=NOW):
    return await pipeline.run_assist(db, bot, MagicMock(), _client(), ticket_id, 'new_ticket', now)


async def _cards(db) -> list[SupportAssistCard]:
    db.expunge_all()
    return list((await db.execute(select(SupportAssistCard).order_by(SupportAssistCard.id))).scalars())


async def _system_messages(db) -> list[TicketMessage]:
    db.expunge_all()
    rows = await db.execute(select(TicketMessage).where(TicketMessage.is_system.is_(True)))
    return list(rows.scalars())


def _spy_model_input(monkeypatch) -> list[list[str]]:
    seen: list[list[str]] = []
    real = pipeline.generate_card

    async def spy(client, payload, texts):
        seen.append(list(texts))
        return await real(client, payload, texts)

    monkeypatch.setattr(pipeline, 'generate_card', spy)
    return seen


@pytest.mark.asyncio
@pytest.mark.parametrize('auto_ask', [False, True])
async def test_rollback_after_snapshot_timeout_still_posts_card(monkeypatch, bot, notify, cabinet, auto_ask):
    """Источник упал, commit после него не прошёл, end_transaction откатил сессию: карточка всё равно уходит."""
    monkeypatch.setattr(settings, 'SUPPORT_ASSIST_AUTO_ASK_ENABLED', auto_ask)
    monkeypatch.setattr(pipeline, 'SNAPSHOT_TIMEOUT_SECONDS', 0.05)

    async def hang(db, api, ticket, now):
        await asyncio.sleep(3600)

    monkeypatch.setattr(pipeline, 'collect_snapshot', hang)
    model_input = _spy_model_input(monkeypatch)

    async with memory_session(monkeypatch, _TABLES) as db:
        ticket_id = await _seed(db)
        real_commit = db.commit
        commits: list[int] = []

        async def flaky_commit():
            commits.append(1)
            if len(commits) == 1:
                raise RuntimeError('PendingRollbackError')  # первый commit после сбоя сбора
            await real_commit()

        monkeypatch.setattr(db, 'commit', flaky_commit)
        rolled_back = AsyncMock(wraps=db.rollback)
        monkeypatch.setattr(db, 'rollback', rolled_back)

        card = await asyncio.wait_for(_run(db, bot, ticket_id), 5)

        rolled_back.assert_awaited()  # сессия действительно откатывалась
        assert card is not None and card.status == 'shown'
        bot.send_message.assert_awaited_once()
        assert model_input == [[CLIENT_TEXT]]  # текст клиента дошёл до модели
        assert notify.await_count == (1 if auto_ask else 0)
        if auto_ask:
            delivered_to = notify.call_args.args[1]
            assert delivered_to.id == ticket_id and delivered_to.user.language == 'ru'  # свежий ORM-тикет
            assert cabinet.emit.await_count == 1 and cabinet.push.await_count == 1
        assert [c.status for c in await _cards(db)] == ['shown']
        assert len(await _system_messages(db)) == (1 if auto_ask else 0)


@pytest.mark.asyncio
async def test_claim_lost_on_integrity_error_sends_nothing_but_posts_card(monkeypatch, bot, notify, cabinet):
    """Второй процесс уже спросил: уникальный индекс откатывает сессию, клиенту ничего, владельцу карточка."""
    monkeypatch.setattr(settings, 'SUPPORT_ASSIST_AUTO_ASK_ENABLED', True)
    monkeypatch.setattr(settings, 'SUPPORT_ASSIST_RECOMPUTE_MINUTES', 0)
    model_input = _spy_model_input(monkeypatch)

    async with memory_session(monkeypatch, _TABLES) as db:
        ticket_id = await _seed(db)
        first = await _run(db, bot, ticket_id)
        assert first is not None and first.status == 'shown'
        notify.assert_awaited_once()

        second = await _run(db, bot, ticket_id, now=NOW + timedelta(seconds=1))

        assert second is not None and second.status == 'shown'
        assert notify.await_count == 1  # вопросы второй раз не ушли
        assert cabinet.emit.await_count == 1 and cabinet.push.await_count == 1
        assert bot.send_message.await_count == 2  # а карточка ушла
        assert model_input == [[CLIENT_TEXT], [CLIENT_TEXT]]
        cards = await _cards(db)
        assert [c.status for c in cards] == ['shown', 'shown']
        assert [c.auto_ask_sent_at is not None for c in cards] == [True, False]
        assert len(await _system_messages(db)) == 1
