"""Карточка ассистента поддержки в админском тикете кабинета: показ, «Отправить» и «Пропустить».

Роуты идут через настоящее приложение и настоящую SQLite-сессию: гонку двух решений (кабинет и
Telegram, два админа) решает один условный UPDATE, и подменой его не проверить. Права проверяются
через настоящую зависимость ``require_permission`` с подменённым ``PermissionService``.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from aiogram.exceptions import TelegramBadRequest
from fastapi import FastAPI
from sqlalchemy import select

from app.cabinet.dependencies import get_cabinet_db, get_current_cabinet_user
from app.cabinet.routes import admin_ticket_assist as route
from app.config import Settings, settings
from app.database.models import SupportAssistCard, Ticket, TicketMessage, User
from app.handlers.admin import support_assist as telegram_buttons, tickets as bot_tickets
from app.services.event_emitter import event_emitter
from app.services.permission_service import PermissionService
from app.services.support_assist import decision
from app.services.support_assist.form_options import fallback_options
from tests.fixtures.sqlite_memory import memory_session


_TABLES = (User.__table__, Ticket.__table__, TicketMessage.__table__, SupportAssistCard.__table__)

CLIENT_TG = 1000
ADMIN_TG = 5000
GROUP_CHAT = -100500
CARD_MESSAGE = 555

DRAFT = 'Удалите лишние устройства в приложении и повторите вход.'
OWNER_TEXT = 'Здравствуйте! Сбросил устройства, попробуйте войти снова.'
CARD = {
    'facts': [{'icon': '⚠️', 'text': 'Устройств 3 из 3'}],
    'cause': 'Лимит устройств',
    'owner_action': 'Сбросить устройства',
    'ask_client': ['Какое приложение?'],
    'draft': DRAFT,
}
FLAGS = [
    {'code': 'devices_full', 'sev': 'mid', 'text': 'Устройств 3 из 3'},
    {'code': 'sub_expired', 'sev': 'high', 'text': 'Подписка истекла'},
]
FORM = {
    'location': 'russia',
    'platform': 'ios',
    'app': 'Happ',
    'network': 'wifi',
    'symptom': 'nothing',
    'since': 'today',
}


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setattr(settings, 'SUPPORT_ASSIST_SEND_BUTTONS_ENABLED', True)
    monkeypatch.setattr(Settings, 'get_admin_notifications_chat_id', lambda self: GROUP_CHAT)
    monkeypatch.setattr(event_emitter, 'emit', AsyncMock())
    # Варианты анкеты без обращения к панели Remnawave.
    monkeypatch.setattr(route, 'get_form_options', AsyncMock(return_value=fallback_options()))
    notify = AsyncMock()
    monkeypatch.setattr(bot_tickets, 'notify_user_about_ticket_reply', notify)
    in_cabinet = AsyncMock(return_value=object())
    push = AsyncMock()
    monkeypatch.setattr(
        'app.database.crud.ticket_notification.TicketNotificationCRUD.create_user_notification_for_admin_reply',
        in_cabinet,
    )
    monkeypatch.setattr('app.cabinet.routes.websocket.notify_user_ticket_reply', push)
    permission = AsyncMock(return_value=(True, ''))
    monkeypatch.setattr(PermissionService, 'check_permission', permission)
    monkeypatch.setattr(PermissionService, 'log_action', AsyncMock())
    return SimpleNamespace(notify=notify, in_cabinet=in_cabinet, push=push, permission=permission)


@pytest.fixture
def notify(env):
    return env.notify


@pytest.fixture
def bot(monkeypatch):
    mock = MagicMock()
    mock.edit_message_text = AsyncMock()
    mock.edit_message_reply_markup = AsyncMock()
    monkeypatch.setattr(route, 'notification_bot', lambda: mock)
    return mock


def _card_row(card_id: int = 5, *, ticket_id: int = 1, status: str = 'shown', card=CARD, **over) -> SupportAssistCard:
    fields = dict(
        id=card_id,
        ticket_id=ticket_id,
        created_at=datetime.now(UTC),
        trigger='new_ticket',
        facts={},
        flags=FLAGS,
        card=card,
        status=status,
        admin_message_id=CARD_MESSAGE,
    )
    fields.update(over)
    return SupportAssistCard(**fields)


@contextlib.asynccontextmanager
async def world(monkeypatch, *, cards=None, ticket_status='open', diagnostics=None, admin_telegram_id=ADMIN_TG):
    """Клиент, админ, тикет №1 с сообщением клиента, тикет №2 другого клиента и карточки.

    По умолчанию — одна карточка №5 с черновиком на тикете №1. ``cards`` заменяет список целиком.
    """
    async with memory_session(monkeypatch, _TABLES) as db:
        db.add_all(
            [
                User(id=1, telegram_id=CLIENT_TG, language='ru'),
                User(id=2, telegram_id=admin_telegram_id, language='ru', email='admin@example.com'),
                User(id=3, telegram_id=1001, language='ru'),
            ]
        )
        db.add(Ticket(id=1, user_id=1, title='t', status=ticket_status, diagnostics=diagnostics))
        db.add(TicketMessage(ticket_id=1, user_id=1, message_text='не работает', is_from_admin=False, has_media=False))
        db.add(Ticket(id=2, user_id=3, title='t2', status='open'))
        db.add_all(cards if cards is not None else [_card_row()])
        await db.commit()
        db.expunge_all()

        app = FastAPI()
        app.include_router(route.router, prefix='/cabinet')
        app.dependency_overrides[get_cabinet_db] = lambda: db
        admin = SimpleNamespace(id=2, telegram_id=admin_telegram_id)
        app.dependency_overrides[get_current_cabinet_user] = lambda: admin
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://cabinet.example') as http:
            yield SimpleNamespace(db=db, http=http)


async def _card(db, card_id: int = 5) -> SupportAssistCard:
    db.expunge_all()
    return (await db.execute(select(SupportAssistCard).where(SupportAssistCard.id == card_id))).scalar_one()


async def _admin_messages(db, ticket_id: int = 1) -> list[TicketMessage]:
    db.expunge_all()
    rows = await db.execute(
        select(TicketMessage).where(TicketMessage.is_from_admin.is_(True), TicketMessage.ticket_id == ticket_id)
    )
    return list(rows.scalars())


async def _ticket_status(db, ticket_id: int = 1) -> str:
    db.expunge_all()
    return (await db.execute(select(Ticket.status).where(Ticket.id == ticket_id))).scalar_one()


def _assist(ticket_id: int = 1) -> str:
    return f'/cabinet/admin/tickets/{ticket_id}/assist'


def _send(card_id: int = 5, ticket_id: int = 1) -> str:
    return f'{_assist(ticket_id)}/{card_id}/send'


def _skip(card_id: int = 5, ticket_id: int = 1) -> str:
    return f'{_assist(ticket_id)}/{card_id}/skip'


# --- GET: что показать -----------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_without_a_card(monkeypatch, bot):
    async with world(monkeypatch, cards=[]) as w:
        response = await w.http.get(_assist())

    assert response.status_code == 200, response.text
    assert response.json() == {'card': None, 'can_send': False, 'diagnostics': None}


@pytest.mark.asyncio
async def test_get_unknown_ticket_is_404(monkeypatch, bot):
    async with world(monkeypatch) as w:
        response = await w.http.get(_assist(99))

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_get_model_card(monkeypatch, bot):
    async with world(monkeypatch) as w:
        response = await w.http.get(_assist())

    assert response.status_code == 200, response.text
    body = response.json()
    card = body['card']
    assert card['id'] == 5
    assert card['status'] == 'shown'
    assert (card['decided'], card['no_model']) == (False, False)
    assert card['facts'] == [{'icon': '⚠️', 'text': 'Устройств 3 из 3'}]
    assert card['cause'] == 'Лимит устройств'
    assert card['owner_action'] == 'Сбросить устройства'
    assert card['ask_client'] == ['Какое приложение?']
    assert card['draft'] == DRAFT
    # Дата с зоной: без неё фронт покажет UTC как местное время.
    assert card['created_at'].endswith(('Z', '+00:00'))
    assert body['can_send'] is True


@pytest.mark.asyncio
async def test_get_returns_the_latest_card_of_the_ticket(monkeypatch, bot):
    newer = _card_row(7, card=dict(CARD, draft='Новый черновик'))
    async with world(monkeypatch, cards=[_card_row(5, status='dismissed'), newer]) as w:
        response = await w.http.get(_assist())

    assert response.json()['card']['id'] == 7
    assert response.json()['card']['draft'] == 'Новый черновик'


@pytest.mark.asyncio
async def test_get_ignores_cards_of_other_tickets(monkeypatch, bot):
    async with world(monkeypatch, cards=[_card_row(5), _card_row(9, ticket_id=2)]) as w:
        response = await w.http.get(_assist())

    assert response.json()['card']['id'] == 5


@pytest.mark.asyncio
async def test_get_cannot_send_when_the_flag_is_off(monkeypatch, bot):
    monkeypatch.setattr(settings, 'SUPPORT_ASSIST_SEND_BUTTONS_ENABLED', False)
    async with world(monkeypatch) as w:
        body = (await w.http.get(_assist())).json()

    assert body['card']['draft'] == DRAFT  # показывать можно, отправлять нельзя
    assert body['card']['decided'] is False
    assert body['can_send'] is False


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['sent', 'edited', 'dismissed', 'failed', 'skipped'])
async def test_get_decided_card_is_not_actionable(monkeypatch, bot, status):
    async with world(monkeypatch, cards=[_card_row(status=status)]) as w:
        body = (await w.http.get(_assist())).json()

    assert body['card']['status'] == status
    assert body['card']['decided'] is True
    assert body['can_send'] is False


@pytest.mark.asyncio
async def test_get_cannot_send_an_empty_draft(monkeypatch, bot):
    async with world(monkeypatch, cards=[_card_row(card=dict(CARD, draft='  '))]) as w:
        body = (await w.http.get(_assist())).json()

    assert body['card']['decided'] is False
    assert body['can_send'] is False


@pytest.mark.asyncio
async def test_get_fallback_card_is_built_from_flags(monkeypatch, bot):
    async with world(monkeypatch, cards=[_card_row(status='fallback', card=None)]) as w:
        body = (await w.http.get(_assist())).json()

    card = body['card']
    assert card['no_model'] is True
    assert card['facts'] == [
        {'icon': '⚠️', 'text': 'Устройств 3 из 3'},
        {'icon': '🔴', 'text': 'Подписка истекла'},
    ]
    assert (card['cause'], card['owner_action'], card['draft']) == ('', '', '')
    assert card['ask_client'] == []
    assert card['decided'] is False  # «Пропустить» у такой карточки ещё работает
    assert body['can_send'] is False


@pytest.mark.asyncio
async def test_get_survives_broken_flags_on_a_fallback_card(monkeypatch, bot):
    async with world(monkeypatch, cards=[_card_row(status='fallback', card=None, flags=[{'oops': 1}])]) as w:
        response = await w.http.get(_assist())

    assert response.status_code == 200, response.text
    assert response.json()['card']['facts'] == []


@pytest.mark.asyncio
async def test_get_diagnostics_use_russian_labels(monkeypatch, bot):
    async with world(monkeypatch, diagnostics=FORM) as w:
        body = (await w.http.get(_assist())).json()

    assert body['diagnostics'][0] == {'label': 'Где находится', 'value': 'Россия'}
    assert len(body['diagnostics']) == 6
    assert {'label': 'Приложение', 'value': 'Happ'} in body['diagnostics']


@pytest.mark.asyncio
async def test_get_diagnostics_show_without_a_card(monkeypatch, bot):
    async with world(monkeypatch, cards=[], diagnostics=FORM) as w:
        body = (await w.http.get(_assist())).json()

    assert body['card'] is None
    assert body['diagnostics'][0]['value'] == 'Россия'


@pytest.mark.asyncio
@pytest.mark.parametrize('stored', [None, {}, {'location': 'mars'}, 'junk'])
async def test_get_diagnostics_are_null_without_usable_answers(monkeypatch, bot, stored):
    async with world(monkeypatch, diagnostics=stored) as w:
        body = (await w.http.get(_assist())).json()

    assert body['diagnostics'] is None


# --- POST send -------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_draft_reaches_the_client_like_the_telegram_button(monkeypatch, bot, env):
    async with world(monkeypatch) as w:
        response = await w.http.post(_send(), json={})

        assert response.status_code == 200, response.text
        assert response.json() == {'status': 'sent'}
        sent = await _admin_messages(w.db)
        assert [(m.message_text, m.user_id, m.is_from_admin) for m in sent] == [(DRAFT, 2, True)]
        assert await _ticket_status(w.db) == 'answered'
        row = await _card(w.db)
        assert (row.status, row.decided_by, row.final_text) == ('sent', ADMIN_TG, DRAFT)
        assert row.decided_at is not None
    env.notify.assert_awaited_once()
    assert env.notify.await_args.args[2] == DRAFT


@pytest.mark.asyncio
async def test_send_without_a_body_at_all_sends_the_draft(monkeypatch, bot, env):
    async with world(monkeypatch) as w:
        response = await w.http.post(_send())

        assert response.status_code == 200, response.text
        assert [m.message_text for m in await _admin_messages(w.db)] == [DRAFT]


@pytest.mark.asyncio
async def test_send_edited_text(monkeypatch, bot, env):
    async with world(monkeypatch) as w:
        response = await w.http.post(_send(), json={'text': f'  {OWNER_TEXT}  '})

        assert response.status_code == 200, response.text
        assert response.json() == {'status': 'sent'}
        assert [m.message_text for m in await _admin_messages(w.db)] == [OWNER_TEXT]
        row = await _card(w.db)
        assert (row.status, row.final_text) == ('edited', OWNER_TEXT)
    assert env.notify.await_args.args[2] == OWNER_TEXT


@pytest.mark.asyncio
async def test_send_the_draft_back_as_text_is_not_an_edit(monkeypatch, bot, env):
    async with world(monkeypatch) as w:
        response = await w.http.post(_send(), json={'text': DRAFT})

        assert response.status_code == 200, response.text
        assert (await _card(w.db)).status == 'sent'


@pytest.mark.asyncio
async def test_send_notifies_the_client_in_the_cabinet(monkeypatch, bot, env):
    async with world(monkeypatch) as w:
        response = await w.http.post(_send(), json={})

    assert response.status_code == 200, response.text
    env.in_cabinet.assert_awaited_once()
    assert env.in_cabinet.await_args.args[2] == DRAFT
    env.push.assert_awaited_once()
    assert env.push.await_args.args[:2] == (1, 1)


@pytest.mark.asyncio
async def test_a_failed_cabinet_notification_does_not_fail_the_send(monkeypatch, bot, env):
    env.in_cabinet.side_effect = RuntimeError('notifications table is gone')
    async with world(monkeypatch) as w:
        response = await w.http.post(_send(), json={})

        assert response.status_code == 200, response.text
        assert (await _card(w.db)).status == 'sent'


@pytest.mark.asyncio
async def test_send_marks_the_telegram_card_as_done_from_the_cabinet(monkeypatch, bot, env):
    async with world(monkeypatch) as w:
        response = await w.http.post(_send(), json={})

    assert response.status_code == 200, response.text
    bot.edit_message_text.assert_awaited_once()
    kwargs = bot.edit_message_text.await_args.kwargs
    assert (kwargs['chat_id'], kwargs['message_id']) == (GROUP_CHAT, CARD_MESSAGE)
    assert kwargs['text'].endswith('✅ отправлено из кабинета')
    assert 'Тикет #1' in kwargs['text']
    assert kwargs['reply_markup'].inline_keyboard == []


@pytest.mark.asyncio
async def test_send_edited_text_marks_the_card_as_edited_from_the_cabinet(monkeypatch, bot, env):
    async with world(monkeypatch) as w:
        await w.http.post(_send(), json={'text': OWNER_TEXT})

    assert bot.edit_message_text.await_args.kwargs['text'].endswith('✅ отправлено из кабинета (текст изменён)')


@pytest.mark.asyncio
async def test_send_does_not_touch_telegram_when_the_card_has_no_message(monkeypatch, bot, env):
    async with world(monkeypatch, cards=[_card_row(admin_message_id=None)]) as w:
        response = await w.http.post(_send(), json={})

        assert response.status_code == 200, response.text
        assert (await _card(w.db)).status == 'sent'
    bot.edit_message_text.assert_not_awaited()
    bot.edit_message_reply_markup.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_telegram_failure_while_marking_the_card_does_not_fail_the_send(monkeypatch, bot, env):
    bot.edit_message_text.side_effect = TelegramBadRequest(method=MagicMock(), message='message is too old')
    bot.edit_message_reply_markup.side_effect = RuntimeError('network is down')
    async with world(monkeypatch) as w:
        response = await w.http.post(_send(), json={})

        assert response.status_code == 200, response.text
        assert (await _card(w.db)).status == 'sent'
    env.notify.assert_awaited_once()


@pytest.mark.asyncio
async def test_send_by_an_admin_without_telegram_is_still_attributed(monkeypatch, bot, env):
    async with world(monkeypatch, admin_telegram_id=None) as w:
        response = await w.http.post(_send(), json={})

        assert response.status_code == 200, response.text
        row = await _card(w.db)
        assert row.status == 'sent'
        assert row.decided_by == -2  # id в нашей БД со знаком минус: не путается с настоящим Telegram id


@pytest.mark.asyncio
async def test_send_is_refused_when_the_flag_is_off(monkeypatch, bot, env):
    monkeypatch.setattr(settings, 'SUPPORT_ASSIST_SEND_BUTTONS_ENABLED', False)
    async with world(monkeypatch) as w:
        response = await w.http.post(_send(), json={})

        assert response.status_code == 403
        assert (await _card(w.db)).status == 'shown'
        assert await _admin_messages(w.db) == []
    env.notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_unknown_card_is_404(monkeypatch, bot, env):
    async with world(monkeypatch) as w:
        response = await w.http.post(_send(card_id=99), json={})

    assert response.status_code == 404
    env.notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_a_card_of_another_ticket_is_404(monkeypatch, bot, env):
    async with world(monkeypatch, cards=[_card_row(5), _card_row(9, ticket_id=2)]) as w:
        response = await w.http.post(_send(card_id=9, ticket_id=1), json={})

        assert response.status_code == 404
        assert (await _card(w.db, 9)).status == 'shown'
        assert await _admin_messages(w.db, 1) == [] and await _admin_messages(w.db, 2) == []
    env.notify.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['sent', 'edited', 'dismissed', 'fallback', 'failed'])
async def test_send_a_card_that_is_not_shown_is_409(monkeypatch, bot, env, status):
    async with world(monkeypatch, cards=[_card_row(status=status)]) as w:
        response = await w.http.post(_send(), json={})

        assert response.status_code == 409
        assert (await _card(w.db)).status == status
    env.notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_a_card_that_is_not_the_latest_is_409(monkeypatch, bot, env):
    async with world(monkeypatch, cards=[_card_row(5), _card_row(7)]) as w:
        response = await w.http.post(_send(card_id=5), json={})

        assert response.status_code == 409
        assert (await _card(w.db, 5)).status == 'shown'
        assert await _admin_messages(w.db) == []
    env.notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_second_send_reaches_the_client_once(monkeypatch, bot, env):
    async with world(monkeypatch) as w:
        first = await w.http.post(_send(), json={})
        second = await w.http.post(_send(), json={'text': OWNER_TEXT})

        assert (first.status_code, second.status_code) == (200, 409)
        assert len(await _admin_messages(w.db)) == 1
    env.notify.assert_awaited_once()


@pytest.mark.asyncio
async def test_cabinet_send_and_telegram_tap_cannot_both_send(monkeypatch, bot, env):
    """Оба прочитали карточку как 'shown' до того, как кто-то её занял: решает условный UPDATE."""
    monkeypatch.setattr(Settings, 'is_admin', lambda self, user_id: user_id == ADMIN_TG)
    async with world(monkeypatch) as w:
        stale = await decision.get_card(w.db, 5)
        w.db.expunge_all()
        real_get_card = decision.get_card
        monkeypatch.setattr(decision, 'get_card', AsyncMock(return_value=stale))

        cabinet = await w.http.post(_send(), json={})

        callback = MagicMock()
        callback.data = 'sa:send:5'
        callback.from_user = SimpleNamespace(id=ADMIN_TG, full_name='Админ')
        callback.answer = AsyncMock()
        callback.message = SimpleNamespace(
            chat=SimpleNamespace(id=GROUP_CHAT, type='supergroup'), message_id=CARD_MESSAGE
        )
        admin = await w.db.get(User, 2)
        await telegram_buttons.handle_send(callback, bot, w.db, admin)
        monkeypatch.setattr(decision, 'get_card', real_get_card)

        assert cabinet.status_code == 200
        assert 'уже обработано' in callback.answer.await_args.args[0].lower()
        assert len(await _admin_messages(w.db)) == 1
    env.notify.assert_awaited_once()


@pytest.mark.asyncio
async def test_losing_the_claim_is_409_and_sends_nothing(monkeypatch, bot, env):
    """Другой админ занял карточку между чтением и захватом."""
    async with world(monkeypatch) as w:
        stale = await decision.get_card(w.db, 5)
        w.db.expunge_all()
        monkeypatch.setattr(decision, 'get_card', AsyncMock(return_value=stale))
        monkeypatch.setattr(decision, 'claim_card_decision', AsyncMock(return_value=False))

        response = await w.http.post(_send(), json={})

        assert response.status_code == 409
        assert await _admin_messages(w.db) == []
    env.notify.assert_not_awaited()
    bot.edit_message_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_to_a_closed_ticket_is_409(monkeypatch, bot, env):
    async with world(monkeypatch, ticket_status='closed') as w:
        response = await w.http.post(_send(), json={})

        assert response.status_code == 409
        assert (await _card(w.db)).status == 'shown'
        assert await _ticket_status(w.db) == 'closed'
    env.notify.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'text',
    ['', '   \n', '/start', '  /help me', 'я' * 4001],
    ids=['empty', 'blank', 'command', 'indented command', 'too long'],
)
async def test_send_rejects_bad_text(monkeypatch, bot, env, text):
    async with world(monkeypatch) as w:
        response = await w.http.post(_send(), json={'text': text})

        assert response.status_code == 422
        assert (await _card(w.db)).status == 'shown'
        assert await _admin_messages(w.db) == []
    env.notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_accepts_text_of_exactly_the_limit(monkeypatch, bot, env):
    async with world(monkeypatch) as w:
        response = await w.http.post(_send(), json={'text': 'я' * 4000})

        assert response.status_code == 200, response.text
        assert len((await _admin_messages(w.db))[0].message_text) == 4000


@pytest.mark.asyncio
async def test_send_a_card_without_a_draft_is_422(monkeypatch, bot, env):
    async with world(monkeypatch, cards=[_card_row(card=dict(CARD, draft=''))]) as w:
        response = await w.http.post(_send(), json={})

        assert response.status_code == 422
        assert (await _card(w.db)).status == 'shown'
    env.notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_sends_a_long_draft_that_the_cabinet_showed_whole(monkeypatch, bot, env):
    """В Telegram такой черновик не влезает в карточку и уходит только через «Править»; в кабинете он виден целиком."""
    long_draft = 'я' * 3900
    cut = dict(CARD, draft=long_draft, cause='ы' * 2000)
    async with world(monkeypatch, cards=[_card_row(card=cut)]) as w:
        shown = (await w.http.get(_assist())).json()
        response = await w.http.post(_send(), json={})

        assert shown['card']['draft'] == long_draft and shown['can_send'] is True
        assert response.status_code == 200, response.text
        assert [m.message_text for m in await _admin_messages(w.db)] == [long_draft]


@pytest.mark.asyncio
async def test_failed_reply_after_the_claim_releases_the_card(monkeypatch, bot, env):
    real_add = bot_tickets.TicketMessageCRUD.add_message
    monkeypatch.setattr(bot_tickets.TicketMessageCRUD, 'add_message', AsyncMock(side_effect=RuntimeError('db is gone')))
    async with world(monkeypatch) as w:
        failed = await w.http.post(_send(), json={})

        assert failed.status_code >= 500
        row = await _card(w.db)
        assert (row.status, row.decided_by, row.final_text) == ('shown', None, None)
        assert await _admin_messages(w.db) == []
        bot.edit_message_text.assert_not_awaited()

        monkeypatch.setattr(bot_tickets.TicketMessageCRUD, 'add_message', real_add)
        again = await w.http.post(_send(), json={})

        assert again.status_code == 200, again.text
        assert len(await _admin_messages(w.db)) == 1


# --- POST skip -------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_skip_dismisses_the_card_and_marks_telegram(monkeypatch, bot, env):
    async with world(monkeypatch) as w:
        response = await w.http.post(_skip())

        assert response.status_code == 200, response.text
        assert response.json() == {'status': 'skipped'}
        row = await _card(w.db)
        assert (row.status, row.decided_by) == ('dismissed', ADMIN_TG)
        assert await _admin_messages(w.db) == []
    env.notify.assert_not_awaited()
    kwargs = bot.edit_message_text.await_args.kwargs
    assert (kwargs['chat_id'], kwargs['message_id']) == (GROUP_CHAT, CARD_MESSAGE)
    assert kwargs['text'].endswith('⏭ пропущено из кабинета')
    assert kwargs['reply_markup'].inline_keyboard == []


@pytest.mark.asyncio
async def test_skip_works_with_the_send_flag_off(monkeypatch, bot, env):
    monkeypatch.setattr(settings, 'SUPPORT_ASSIST_SEND_BUTTONS_ENABLED', False)
    async with world(monkeypatch) as w:
        response = await w.http.post(_skip())

        assert response.status_code == 200, response.text
        assert (await _card(w.db)).status == 'dismissed'


@pytest.mark.asyncio
async def test_skip_a_fallback_card(monkeypatch, bot, env):
    async with world(monkeypatch, cards=[_card_row(status='fallback', card=None)]) as w:
        response = await w.http.post(_skip())

        assert response.status_code == 200, response.text
        assert (await _card(w.db)).status == 'dismissed'
    assert bot.edit_message_text.await_args.kwargs['text'].endswith('⏭ пропущено из кабинета')


@pytest.mark.asyncio
async def test_skip_unknown_card_is_404(monkeypatch, bot, env):
    async with world(monkeypatch) as w:
        assert (await w.http.post(_skip(card_id=99))).status_code == 404


@pytest.mark.asyncio
async def test_skip_a_card_of_another_ticket_is_404(monkeypatch, bot, env):
    async with world(monkeypatch, cards=[_card_row(5), _card_row(9, ticket_id=2)]) as w:
        response = await w.http.post(_skip(card_id=9, ticket_id=1))

        assert response.status_code == 404
        assert (await _card(w.db, 9)).status == 'shown'


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['sent', 'edited', 'dismissed', 'failed', 'skipped'])
async def test_skip_a_decided_card_is_409(monkeypatch, bot, env, status):
    async with world(monkeypatch, cards=[_card_row(status=status)]) as w:
        response = await w.http.post(_skip())

        assert response.status_code == 409
        assert (await _card(w.db)).status == status
    bot.edit_message_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_skip_a_card_that_is_not_the_latest_is_409(monkeypatch, bot, env):
    async with world(monkeypatch, cards=[_card_row(5), _card_row(7)]) as w:
        response = await w.http.post(_skip(card_id=5))

        assert response.status_code == 409
        assert (await _card(w.db, 5)).status == 'shown'


@pytest.mark.asyncio
async def test_skip_after_send_is_409(monkeypatch, bot, env):
    async with world(monkeypatch) as w:
        await w.http.post(_send(), json={})
        response = await w.http.post(_skip())

        assert response.status_code == 409
        assert (await _card(w.db)).status == 'sent'


# --- права -----------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('method', 'path', 'permission'),
    [
        ('GET', _assist(), 'tickets:read'),
        ('POST', _send(), 'tickets:reply'),
        ('POST', _skip(), 'tickets:reply'),
    ],
)
async def test_routes_require_a_ticket_permission(monkeypatch, bot, env, method, path, permission):
    env.permission.return_value = (False, 'not allowed')
    async with world(monkeypatch) as w:
        response = await w.http.request(method, path, json={} if method == 'POST' else None)

        assert response.status_code == 403
        assert env.permission.await_args.args[2] == permission
        assert (await _card(w.db)).status == 'shown'
        assert await _admin_messages(w.db) == []
    env.notify.assert_not_awaited()


def test_the_router_is_mounted_in_the_cabinet():
    from app.cabinet.routes import router as cabinet_router

    app = FastAPI()
    app.include_router(cabinet_router)
    operations = {(method, path) for path, item in app.openapi()['paths'].items() for method in item}
    assert ('get', '/cabinet/admin/tickets/{ticket_id}/assist') in operations
    assert ('post', '/cabinet/admin/tickets/{ticket_id}/assist/{card_id}/send') in operations
    assert ('post', '/cabinet/admin/tickets/{ticket_id}/assist/{card_id}/skip') in operations
    # Соседний маршрут тикета не перекрыт новыми.
    assert ('get', '/cabinet/admin/tickets/{ticket_id}') in operations
