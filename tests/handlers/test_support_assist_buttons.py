"""Кнопки карточки ассистента: «Отправить», «Править», «Пропустить».

Карточка лежит в топике тикетов группового админ-чата, поэтому кнопки проходят через
``ChatTypeFilterMiddleware`` (белый список) и не могут вводить текст в группе: «Править» пишет админу
в личку и ждёт его текст в личном FSM. Тесты гоняют обработчики на настоящих строках SQLite: гонку
двух нажатий решает один условный UPDATE, и подменой его не проверить.
"""

from __future__ import annotations

import contextlib
import inspect
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import Dispatcher
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery
from sqlalchemy import select

from app.config import Settings, settings
from app.database.models import SupportAssistCard, Ticket, TicketMessage, User
from app.handlers.admin import support_assist as sa, tickets as admin_tickets
from app.keyboards.group_callbacks import is_group_safe_callback
from app.middlewares.chat_type_filter import ChatTypeFilterMiddleware
from app.services.event_emitter import event_emitter
from app.services.support_assist.render import build_card_keyboard
from app.services.support_settings_service import SupportSettingsService
from app.states import SupportAssistStates
from app.utils.ticket_text import TICKET_MESSAGE_MAX_LENGTH
from tests.fixtures.sqlite_memory import memory_session


_TABLES = (User.__table__, Ticket.__table__, TicketMessage.__table__, SupportAssistCard.__table__)

CLIENT_TG = 1000
ADMIN_TG = 5000
MODERATOR_TG = 5001
STRANGER_TG = 6000
GROUP_CHAT = -100500
TOPIC = 77
CARD_MESSAGE = 555
SECOND_CARD_MESSAGE = 556
SECOND_CLIENT_TG = 1001
BOT_ID = 42

DRAFT = 'Удалите лишние устройства в приложении и повторите вход.'
OWNER_TEXT = 'Здравствуйте! Сбросил устройства, попробуйте войти снова.'
SECOND_DRAFT = 'Проверьте платёж в личном кабинете.'
CARD = {
    'facts': [{'icon': '⚠️', 'text': 'Устройств 3 из 3'}],
    'cause': 'Лимит устройств',
    'owner_action': 'нет',
    'ask_client': ['Какое приложение?'],
    'draft': DRAFT,
}
FLAGS = [{'code': 'devices_full', 'sev': 'mid', 'text': 'Устройств 3 из 3'}]


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setattr(Settings, 'is_admin', lambda self, user_id: user_id == ADMIN_TG)
    monkeypatch.setattr(SupportSettingsService, 'is_moderator', classmethod(lambda cls, tid: tid == MODERATOR_TG))
    monkeypatch.setattr(settings, 'SUPPORT_ASSIST_SEND_BUTTONS_ENABLED', True)
    monkeypatch.setattr(event_emitter, 'emit', AsyncMock())
    notify = AsyncMock()
    monkeypatch.setattr(admin_tickets, 'notify_user_about_ticket_reply', notify)
    return SimpleNamespace(notify=notify)


@pytest.fixture
def notify(env):
    return env.notify


@pytest.fixture
def bot():
    mock = MagicMock()
    mock.id = BOT_ID
    mock.send_message = AsyncMock(return_value=SimpleNamespace(message_id=900))
    mock.edit_message_text = AsyncMock()
    mock.edit_message_reply_markup = AsyncMock()
    return mock


@contextlib.asynccontextmanager
async def world(monkeypatch, *, card_status='shown', card=CARD, ticket_status='open', second_card=False):
    """Клиент, админ, модератор, тикет с одним сообщением клиента и карточка №5 в топике.

    ``second_card``: ещё один клиент с тикетом №2 и карточкой №6 (сообщение 556 в том же топике).
    """
    async with memory_session(monkeypatch, _TABLES) as db:
        db.add_all(
            [
                User(id=1, telegram_id=CLIENT_TG, language='ru'),
                User(id=2, telegram_id=ADMIN_TG, language='ru'),
                User(id=3, telegram_id=MODERATOR_TG, language='ru'),
            ]
        )
        db.add(Ticket(id=1, user_id=1, title='t', status=ticket_status))
        db.add(TicketMessage(ticket_id=1, user_id=1, message_text='не работает', is_from_admin=False, has_media=False))
        db.add(
            SupportAssistCard(
                id=5,
                ticket_id=1,
                created_at=datetime.now(UTC),
                trigger='new_ticket',
                facts={},
                flags=FLAGS,
                card=card,
                status=card_status,
                admin_message_id=CARD_MESSAGE,
            )
        )
        if second_card:
            db.add(User(id=4, telegram_id=SECOND_CLIENT_TG, language='ru'))
            db.add(Ticket(id=2, user_id=4, title='t2', status='open'))
            db.add(
                TicketMessage(ticket_id=2, user_id=4, message_text='не платит', is_from_admin=False, has_media=False)
            )
            db.add(
                SupportAssistCard(
                    id=6,
                    ticket_id=2,
                    created_at=datetime.now(UTC),
                    trigger='new_ticket',
                    facts={},
                    flags=FLAGS,
                    card=dict(CARD, draft=SECOND_DRAFT),
                    status='shown',
                    admin_message_id=SECOND_CARD_MESSAGE,
                )
            )
        await db.commit()
        db.expunge_all()
        yield SimpleNamespace(
            db=db,
            admin=await db.get(User, 2),
            moderator=await db.get(User, 3),
            storage=MemoryStorage(),
        )


def _callback(
    data: str,
    user_id: int,
    *,
    chat_type: str = ChatType.SUPERGROUP,
    message: bool = True,
    message_id: int = CARD_MESSAGE,
) -> MagicMock:
    cb = MagicMock(spec=CallbackQuery)
    cb.data = data
    cb.from_user = SimpleNamespace(id=user_id, full_name='Админ')
    cb.answer = AsyncMock()
    if message:
        cb.message = SimpleNamespace(
            chat=SimpleNamespace(id=GROUP_CHAT if chat_type != ChatType.PRIVATE else user_id, type=chat_type),
            message_id=message_id,
            edit_text=AsyncMock(),
        )
    else:
        cb.message = None
    return cb


def _group_state(w, user_id: int, *, private: bool = False) -> FSMContext:
    if private:
        key = StorageKey(bot_id=BOT_ID, chat_id=user_id, user_id=user_id)
    else:
        key = StorageKey(bot_id=BOT_ID, chat_id=GROUP_CHAT, user_id=user_id, thread_id=TOPIC)
    return FSMContext(storage=w.storage, key=key)


def _private_state(w, user_id: int) -> FSMContext:
    return _group_state(w, user_id, private=True)


def _text_message(text: str | None, user_id: int, *, reply_to: int | None = None) -> MagicMock:
    msg = MagicMock()
    msg.reply_to_message = SimpleNamespace(message_id=reply_to) if reply_to is not None else None
    msg.text = text
    msg.caption = None
    msg.from_user = SimpleNamespace(id=user_id)
    msg.chat = SimpleNamespace(id=user_id, type=ChatType.PRIVATE)
    msg.answer = AsyncMock()
    return msg


def _waiting(**over) -> dict:
    """Данные личного FSM «ждём текст правки» для карточки №5; запрос правки открыт только что."""
    data = {
        'sa_card_id': 5,
        'sa_chat_id': GROUP_CHAT,
        'sa_message_id': CARD_MESSAGE,
        'edit_started_at': time.time(),
    }
    data.update(over)
    return data


def _alert(cb: MagicMock) -> tuple[str | None, bool]:
    """Текст и признак всплывающего окна единственного ответа на нажатие."""
    cb.answer.assert_awaited_once()
    args, kwargs = cb.answer.await_args
    return (args[0] if args else kwargs.get('text')), bool(kwargs.get('show_alert'))


async def _card(db, card_id: int = 5) -> SupportAssistCard:
    db.expunge_all()
    return (await db.execute(select(SupportAssistCard).where(SupportAssistCard.id == card_id))).scalar_one()


async def _admin_messages(db, ticket_id: int | None = None) -> list[TicketMessage]:
    db.expunge_all()
    query = select(TicketMessage).where(TicketMessage.is_from_admin.is_(True))
    if ticket_id is not None:
        query = query.where(TicketMessage.ticket_id == ticket_id)
    rows = await db.execute(query)
    return list(rows.scalars())


async def _ticket_status(db) -> str:
    db.expunge_all()
    return (await db.execute(select(Ticket.status).where(Ticket.id == 1))).scalar_one()


def _card_message_edit(bot) -> dict:
    """Единственная правка сообщения карточки в топике: её аргументы."""
    bot.edit_message_text.assert_awaited_once()
    return bot.edit_message_text.await_args.kwargs


def _has_no_buttons(markup) -> bool:
    return markup is not None and markup.inline_keyboard == []


# --- кнопки проходят фильтр групповых чатов ----------------------------------------------------------------------


@pytest.mark.parametrize('show_send', [True, False])
def test_every_card_button_passes_the_group_filter(show_send):
    buttons = [b for row in build_card_keyboard(5, show_send).inline_keyboard for b in row]

    assert buttons
    dead = [b.callback_data for b in buttons if not is_group_safe_callback(b.callback_data)]
    assert not dead, f'кнопки карточки нарисованы в группе, но фильтр их выбросит: {dead}'


@pytest.mark.asyncio
@pytest.mark.parametrize('data', ['sa:send:5', 'sa:edit:5', 'sa:skip:5'])
async def test_middleware_lets_card_callbacks_through_from_a_supergroup(data):
    event = MagicMock(spec=CallbackQuery)
    event.data = data
    event.message = MagicMock()
    event.message.chat = MagicMock()
    event.message.chat.type = ChatType.SUPERGROUP
    handler = AsyncMock(return_value='handled')

    assert await ChatTypeFilterMiddleware()(handler, event, {}) == 'handled'


@pytest.mark.asyncio
async def test_middleware_still_drops_the_private_only_cancel_in_a_group():
    event = MagicMock(spec=CallbackQuery)
    event.data = 'sa:cancel:5'
    event.message = MagicMock()
    event.message.chat = MagicMock()
    event.message.chat.type = ChatType.SUPERGROUP
    handler = AsyncMock(return_value='handled')

    assert await ChatTypeFilterMiddleware()(handler, event, {}) is None
    handler.assert_not_awaited()


# --- доступ и флаг -------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['send', 'edit', 'skip'])
async def test_non_admin_is_refused(monkeypatch, bot, notify, action):
    async with world(monkeypatch) as w:
        cb = _callback(f'sa:{action}:5', STRANGER_TG)
        stranger = SimpleNamespace(id=99)
        if action == 'send':
            await sa.handle_send(cb, bot, w.db, stranger)
        elif action == 'edit':
            await sa.handle_edit(cb, _group_state(w, STRANGER_TG), bot, w.db)
        else:
            await sa.handle_skip(cb, bot, w.db)

        text, is_alert = _alert(cb)
        assert text == 'Нет доступа' and is_alert
        assert (await _card(w.db)).status == 'shown'
        notify.assert_not_awaited()
        bot.send_message.assert_not_awaited()
        assert await _admin_messages(w.db) == []


@pytest.mark.asyncio
async def test_moderator_may_send(monkeypatch, bot, notify):
    async with world(monkeypatch) as w:
        cb = _callback('sa:send:5', MODERATOR_TG)

        await sa.handle_send(cb, bot, w.db, w.moderator)

        notify.assert_awaited_once()
        row = await _card(w.db)
        assert (row.status, row.decided_by) == ('sent', MODERATOR_TG)


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['send', 'edit'])
async def test_send_and_edit_are_refused_when_the_buttons_flag_is_off(monkeypatch, bot, notify, action):
    monkeypatch.setattr(settings, 'SUPPORT_ASSIST_SEND_BUTTONS_ENABLED', False)
    async with world(monkeypatch) as w:
        cb = _callback(f'sa:{action}:5', ADMIN_TG)
        if action == 'send':
            await sa.handle_send(cb, bot, w.db, w.admin)
        else:
            await sa.handle_edit(cb, _group_state(w, ADMIN_TG), bot, w.db)

        text, is_alert = _alert(cb)
        assert 'выключен' in text and is_alert
        assert (await _card(w.db)).status == 'shown'
        notify.assert_not_awaited()
        bot.send_message.assert_not_awaited()
        assert await _admin_messages(w.db) == []


@pytest.mark.asyncio
async def test_skip_still_works_when_the_buttons_flag_is_off(monkeypatch, bot, notify):
    monkeypatch.setattr(settings, 'SUPPORT_ASSIST_SEND_BUTTONS_ENABLED', False)
    async with world(monkeypatch) as w:
        await sa.handle_skip(_callback('sa:skip:5', ADMIN_TG), bot, w.db)

        assert (await _card(w.db)).status == 'dismissed'


# --- отправить -----------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_delivers_the_draft_exactly_like_a_manual_reply(monkeypatch, bot, notify):
    async with world(monkeypatch) as w:
        cb = _callback('sa:send:5', ADMIN_TG)

        await sa.handle_send(cb, bot, w.db, w.admin)

        (reply,) = await _admin_messages(w.db)
        assert (reply.message_text, reply.user_id, reply.is_from_admin) == (DRAFT, 2, True)
        assert reply.is_system is not True  # это реплика владельца, а не системы
        assert await _ticket_status(w.db) == 'answered'  # статус ставит add_message, как при ручном ответе
        notify.assert_awaited_once()
        assert notify.await_args.args[0] is bot
        assert notify.await_args.args[1].id == 1
        assert notify.await_args.args[2] == DRAFT
        row = await _card(w.db)
        assert (row.status, row.final_text, row.decided_by) == ('sent', DRAFT, ADMIN_TG)
        assert row.decided_at is not None
        text, is_alert = _alert(cb)
        assert text == 'Отправлено' and not is_alert


@pytest.mark.asyncio
async def test_send_marks_the_card_in_the_group_and_removes_the_buttons(monkeypatch, bot, notify):
    async with world(monkeypatch) as w:
        await sa.handle_send(_callback('sa:send:5', ADMIN_TG), bot, w.db, w.admin)

        edit = _card_message_edit(bot)
        assert (edit['chat_id'], edit['message_id']) == (GROUP_CHAT, CARD_MESSAGE)
        assert edit['text'].endswith('✅ отправлено')
        assert 'Тикет #1' in edit['text'] and 'Лимит устройств' in edit['text']  # карточка не потеряна
        assert len(edit['text']) <= 4096
        assert edit['parse_mode'] == 'HTML'
        assert _has_no_buttons(edit['reply_markup'])


@pytest.mark.asyncio
async def test_a_huge_card_still_fits_the_limit_with_the_decision_line(monkeypatch, bot, notify):
    """Огромная карточка: отправить можно только свой текст через «Править», строка решения в лимит помещается."""
    huge = dict(CARD, draft='я' * 3900, cause='ы' * 2000)
    async with world(monkeypatch, card=huge) as w:
        private = _private_state(w, ADMIN_TG)
        await private.set_state(SupportAssistStates.waiting_edit)
        await private.set_data(_waiting())

        await sa.handle_edit_text(_text_message(OWNER_TEXT, ADMIN_TG), private, bot, w.db, w.admin)

        text = _card_message_edit(bot)['text']
        assert len(text) <= 4096 and text.endswith('✅ отправлено (текст изменён)')


# --- «Отправить» не шлёт то, чего владелец не видел ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_refuses_a_draft_that_did_not_fit_the_card(monkeypatch, bot, notify):
    """Черновик в пределах 4000 знаков, но в карточку (4096 с остальным) не влез: владелец видел обрубок."""
    cut = dict(CARD, draft='я' * 3900, cause='ы' * 2000)
    async with world(monkeypatch, card=cut) as w:
        cb = _callback('sa:send:5', ADMIN_TG)

        await sa.handle_send(cb, bot, w.db, w.admin)

        text, is_alert = _alert(cb)
        assert is_alert and 'Править' in text and 'целиком' in text
        assert (await _card(w.db)).status == 'shown'
        assert await _admin_messages(w.db) == []
        notify.assert_not_awaited()
        bot.edit_message_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_still_works_for_a_draft_that_fit_in_full(monkeypatch, bot, notify):
    roomy = dict(CARD, draft='Я' * 1500, cause='ы' * 500)
    async with world(monkeypatch, card=roomy) as w:
        await sa.handle_send(_callback('sa:send:5', ADMIN_TG), bot, w.db, w.admin)

        (reply,) = await _admin_messages(w.db)
        assert reply.message_text == 'Я' * 1500
        notify.assert_awaited_once()


@pytest.mark.asyncio
async def test_send_refuses_when_the_card_cannot_be_rebuilt(monkeypatch, bot, notify):
    """Не удалось собрать карточку заново — не доказать, что владелец видел черновик: не отправляем."""
    broken = dict(CARD, facts='not a list')
    async with world(monkeypatch, card=broken) as w:
        cb = _callback('sa:send:5', ADMIN_TG)

        await sa.handle_send(cb, bot, w.db, w.admin)

        assert _alert(cb)[1] is True
        assert (await _card(w.db)).status == 'shown'
        notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_edit_stays_available_for_a_draft_that_did_not_fit(monkeypatch, bot, notify):
    cut = dict(CARD, draft='я' * 3900, cause='ы' * 2000)
    async with world(monkeypatch, card=cut) as w:
        cb = _callback('sa:edit:5', ADMIN_TG)

        await sa.handle_edit(cb, _group_state(w, ADMIN_TG), bot, w.db)

        assert await _private_state(w, ADMIN_TG).get_state() == SupportAssistStates.waiting_edit.state
        bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_double_tap_reaches_the_client_once(monkeypatch, bot, notify):
    async with world(monkeypatch) as w:
        first = _callback('sa:send:5', ADMIN_TG)
        second = _callback('sa:send:5', ADMIN_TG)

        await sa.handle_send(first, bot, w.db, w.admin)
        await sa.handle_send(second, bot, w.db, w.admin)

        notify.assert_awaited_once()
        assert len(await _admin_messages(w.db)) == 1
        text, is_alert = _alert(second)
        assert 'уже обработано' in text.lower() and is_alert


@pytest.mark.asyncio
async def test_two_admins_pressing_at_once_reach_the_client_once(monkeypatch, bot, notify):
    """Оба прочитали карточку как 'shown' до того, как кто-то её занял: решает условный UPDATE."""
    async with world(monkeypatch) as w:
        stale = await sa.get_card(w.db, 5)
        w.db.expunge_all()
        monkeypatch.setattr(sa, 'get_card', AsyncMock(return_value=stale))
        first = _callback('sa:send:5', ADMIN_TG)
        second = _callback('sa:send:5', MODERATOR_TG)

        await sa.handle_send(first, bot, w.db, w.admin)
        await sa.handle_send(second, bot, w.db, w.moderator)

        notify.assert_awaited_once()
        assert len(await _admin_messages(w.db)) == 1
        row = await _card(w.db)
        assert (row.status, row.decided_by) == ('sent', ADMIN_TG)
        assert 'уже обработано' in _alert(second)[0].lower()
        bot.edit_message_text.assert_awaited_once()  # проигравший сообщение карточки не трогает


@pytest.mark.asyncio
async def test_failed_reply_after_a_claim_returns_the_card_to_shown(monkeypatch, bot, notify):
    async with world(monkeypatch) as w:
        real_add = admin_tickets.TicketMessageCRUD.add_message
        monkeypatch.setattr(
            admin_tickets.TicketMessageCRUD, 'add_message', AsyncMock(side_effect=RuntimeError('db is gone'))
        )
        cb = _callback('sa:send:5', ADMIN_TG)

        await sa.handle_send(cb, bot, w.db, w.admin)

        row = await _card(w.db)
        assert (row.status, row.decided_by, row.decided_at, row.final_text) == ('shown', None, None, None)
        notify.assert_not_awaited()
        bot.edit_message_text.assert_not_awaited()  # кнопки на месте, можно нажать ещё раз
        assert await _admin_messages(w.db) == []
        text, is_alert = _alert(cb)
        assert 'не удалось' in text.lower() and is_alert

        # карточка действительно свободна: повторное нажатие доходит до клиента
        monkeypatch.setattr(admin_tickets.TicketMessageCRUD, 'add_message', real_add)
        await sa.handle_send(_callback('sa:send:5', ADMIN_TG), bot, w.db, w.admin)
        notify.assert_awaited_once()
        assert (await _card(w.db)).status == 'sent'


@pytest.mark.asyncio
async def test_send_into_a_closed_ticket_is_refused_and_does_not_reopen_it(monkeypatch, bot, notify):
    async with world(monkeypatch, ticket_status='closed') as w:
        cb = _callback('sa:send:5', ADMIN_TG)

        await sa.handle_send(cb, bot, w.db, w.admin)

        text, is_alert = _alert(cb)
        assert 'закрыт' in text.lower() and is_alert
        assert (await _card(w.db)).status == 'shown'
        assert await _ticket_status(w.db) == 'closed'
        notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_draft_over_the_ticket_limit_is_not_sent(monkeypatch, bot, notify):
    long_card = dict(CARD, draft='я' * (TICKET_MESSAGE_MAX_LENGTH + 1))
    async with world(monkeypatch, card=long_card) as w:
        cb = _callback('sa:send:5', ADMIN_TG)

        await sa.handle_send(cb, bot, w.db, w.admin)

        text, is_alert = _alert(cb)
        assert 'Править' in text and is_alert
        assert (await _card(w.db)).status == 'shown'
        notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_card_without_a_draft_cannot_be_sent(monkeypatch, bot, notify):
    async with world(monkeypatch, card=None) as w:
        cb = _callback('sa:send:5', ADMIN_TG)

        await sa.handle_send(cb, bot, w.db, w.admin)

        assert 'черновик' in _alert(cb)[0].lower()
        assert (await _card(w.db)).status == 'shown'
        notify.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('data', ['sa:send:abc', 'sa:send:', 'sa:send:99'])
async def test_unknown_card_is_reported_not_crashed(monkeypatch, bot, notify, data):
    async with world(monkeypatch) as w:
        cb = _callback(data, ADMIN_TG)

        await sa.handle_send(cb, bot, w.db, w.admin)

        assert _alert(cb)[1] is True
        notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_decision_survives_a_failing_card_message_edit(monkeypatch, bot, notify):
    """Сообщение в группе уже нельзя править (старше 48 часов, удалено): клиент ответ всё равно получил."""
    async with world(monkeypatch) as w:
        bot.edit_message_text.side_effect = TelegramBadRequest(method=MagicMock(), message='message to edit not found')
        cb = _callback('sa:send:5', ADMIN_TG)

        await sa.handle_send(cb, bot, w.db, w.admin)

        notify.assert_awaited_once()
        assert (await _card(w.db)).status == 'sent'
        bot.edit_message_reply_markup.assert_awaited_once()  # хотя бы кнопки снимаем
        assert _alert(cb)[0] == 'Отправлено'


# --- править -------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize('where', ['group', 'private'])
async def test_edit_sends_the_owner_text_not_the_draft(monkeypatch, bot, notify, where):
    async with world(monkeypatch) as w:
        in_private = where == 'private'
        chat_type = ChatType.PRIVATE if in_private else ChatType.SUPERGROUP
        pressed_in = _group_state(w, ADMIN_TG, private=in_private)
        cb = _callback('sa:edit:5', ADMIN_TG, chat_type=chat_type)
        before = time.time()

        await sa.handle_edit(cb, pressed_in, bot, w.db)

        # админу в личку уходит черновик и просьба прислать свой текст
        bot.send_message.assert_awaited_once()
        dm = bot.send_message.await_args.kwargs
        assert dm['chat_id'] == ADMIN_TG and dm['parse_mode'] == 'HTML'
        assert DRAFT in dm['text'] and 'по тикету #1' in dm['text']
        assert dm['reply_markup'].inline_keyboard[0][0].callback_data == 'sa:cancel:5'
        # ждём текст в ЛИЧНОМ FSM админа, а не в групповом
        private = _private_state(w, ADMIN_TG)
        assert await private.get_state() == SupportAssistStates.waiting_edit.state
        data = await private.get_data()
        started = data.pop('edit_started_at')  # когда открыт запрос: правка живёт ограниченное время
        assert before <= started <= time.time()
        assert data == {
            'sa_card_id': 5,
            'sa_chat_id': cb.message.chat.id,
            'sa_message_id': CARD_MESSAGE,
            'sa_prompt_id': 900,
        }
        if not in_private:
            assert await pressed_in.get_state() is None
        # карточка пока не решена, клиенту ничего не ушло
        assert (await _card(w.db)).status == 'shown'
        notify.assert_not_awaited()
        assert _alert(cb)[1] is False

        message = _text_message(OWNER_TEXT, ADMIN_TG)
        await sa.handle_edit_text(message, private, bot, w.db, w.admin)

        (reply,) = await _admin_messages(w.db)
        assert (reply.message_text, reply.user_id) == (OWNER_TEXT, 2)
        notify.assert_awaited_once()
        assert notify.await_args.args[2] == OWNER_TEXT
        row = await _card(w.db)
        assert (row.status, row.final_text, row.decided_by) == ('edited', OWNER_TEXT, ADMIN_TG)
        assert await private.get_state() is None
        edit = _card_message_edit(bot)
        assert (edit['chat_id'], edit['message_id']) == (cb.message.chat.id, CARD_MESSAGE)
        assert edit['text'].endswith('✅ отправлено (текст изменён)')
        assert _has_no_buttons(edit['reply_markup'])
        message.answer.assert_awaited_once()
        assert '#1' in message.answer.await_args.args[0]


@pytest.mark.asyncio
async def test_edit_with_closed_dm_alerts_and_leaves_the_card_shown(monkeypatch, bot, notify):
    async with world(monkeypatch) as w:
        bot.send_message.side_effect = TelegramForbiddenError(
            method=MagicMock(), message="Forbidden: bot can't initiate conversation with a user"
        )
        group = _group_state(w, ADMIN_TG)
        cb = _callback('sa:edit:5', ADMIN_TG)

        await sa.handle_edit(cb, group, bot, w.db)

        text, is_alert = _alert(cb)
        assert is_alert and 'личн' in text.lower() and '/start' in text
        assert await _private_state(w, ADMIN_TG).get_state() is None
        assert await group.get_state() is None
        assert (await _card(w.db)).status == 'shown'
        notify.assert_not_awaited()
        bot.edit_message_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_edit_on_a_decided_card_does_not_open_a_dialog(monkeypatch, bot, notify):
    async with world(monkeypatch, card_status='sent') as w:
        cb = _callback('sa:edit:5', ADMIN_TG)

        await sa.handle_edit(cb, _group_state(w, ADMIN_TG), bot, w.db)

        assert 'уже обработано' in _alert(cb)[0].lower()
        bot.send_message.assert_not_awaited()
        assert await _private_state(w, ADMIN_TG).get_state() is None


@pytest.mark.asyncio
async def test_edit_prompt_with_a_huge_draft_fits_one_telegram_message(monkeypatch, bot, notify):
    big = dict(CARD, draft='я<' * 2000)
    async with world(monkeypatch, card=big) as w:
        await sa.handle_edit(_callback('sa:edit:5', ADMIN_TG), _group_state(w, ADMIN_TG), bot, w.db)

        assert len(bot.send_message.await_args.kwargs['text']) <= 4096


@pytest.mark.asyncio
@pytest.mark.parametrize('bad', [None, '', '   ', 'я' * (TICKET_MESSAGE_MAX_LENGTH + 1)])
async def test_edit_text_rejects_empty_and_too_long_and_keeps_waiting(monkeypatch, bot, notify, bad):
    async with world(monkeypatch) as w:
        private = _private_state(w, ADMIN_TG)
        await private.set_state(SupportAssistStates.waiting_edit)
        await private.set_data(_waiting())
        message = _text_message(bad, ADMIN_TG)

        await sa.handle_edit_text(message, private, bot, w.db, w.admin)

        message.answer.assert_awaited_once()
        assert await private.get_state() == SupportAssistStates.waiting_edit.state
        assert (await _card(w.db)).status == 'shown'
        notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_edit_text_after_someone_else_decided_sends_nothing(monkeypatch, bot, notify):
    async with world(monkeypatch, card_status='dismissed') as w:
        private = _private_state(w, ADMIN_TG)
        await private.set_state(SupportAssistStates.waiting_edit)
        await private.set_data(_waiting())
        message = _text_message(OWNER_TEXT, ADMIN_TG)

        await sa.handle_edit_text(message, private, bot, w.db, w.admin)

        assert 'уже обработано' in message.answer.await_args.args[0].lower()
        assert await private.get_state() is None
        notify.assert_not_awaited()
        assert await _admin_messages(w.db) == []


@pytest.mark.asyncio
async def test_edit_text_failure_keeps_the_dialog_and_the_card_free(monkeypatch, bot, notify):
    async with world(monkeypatch) as w:
        private = _private_state(w, ADMIN_TG)
        await private.set_state(SupportAssistStates.waiting_edit)
        await private.set_data(_waiting())
        monkeypatch.setattr(
            admin_tickets.TicketMessageCRUD, 'add_message', AsyncMock(side_effect=RuntimeError('db is gone'))
        )
        message = _text_message(OWNER_TEXT, ADMIN_TG)

        await sa.handle_edit_text(message, private, bot, w.db, w.admin)

        assert (await _card(w.db)).status == 'shown'
        assert await private.get_state() == SupportAssistStates.waiting_edit.state  # можно прислать текст ещё раз
        assert 'не удалось' in message.answer.await_args.args[0].lower()
        notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_edit_text_from_an_admin_who_lost_access_is_refused(monkeypatch, bot, notify):
    async with world(monkeypatch) as w:
        private = _private_state(w, STRANGER_TG)
        await private.set_state(SupportAssistStates.waiting_edit)
        await private.set_data(_waiting())
        message = _text_message(OWNER_TEXT, STRANGER_TG)

        await sa.handle_edit_text(message, private, bot, w.db, SimpleNamespace(id=99))

        assert message.answer.await_args.args[0] == 'Нет доступа'
        assert await private.get_state() is None
        assert (await _card(w.db)).status == 'shown'
        notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_edit_text_with_the_flag_turned_off_meanwhile_is_refused(monkeypatch, bot, notify):
    async with world(monkeypatch) as w:
        private = _private_state(w, ADMIN_TG)
        await private.set_state(SupportAssistStates.waiting_edit)
        await private.set_data(_waiting())
        monkeypatch.setattr(settings, 'SUPPORT_ASSIST_SEND_BUTTONS_ENABLED', False)
        message = _text_message(OWNER_TEXT, ADMIN_TG)

        await sa.handle_edit_text(message, private, bot, w.db, w.admin)

        assert 'выключен' in message.answer.await_args.args[0]
        assert await private.get_state() is None
        notify.assert_not_awaited()


# --- правка живёт ограниченное время; команда не уходит клиенту -------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize('age_minutes', [31, 24 * 60])
async def test_expired_edit_sends_nothing_and_clears_the_state(monkeypatch, bot, notify, age_minutes):
    async with world(monkeypatch) as w:
        private = _private_state(w, ADMIN_TG)
        await private.set_state(SupportAssistStates.waiting_edit)
        await private.set_data(_waiting(edit_started_at=time.time() - age_minutes * 60))
        message = _text_message(OWNER_TEXT, ADMIN_TG)

        await sa.handle_edit_text(message, private, bot, w.db, w.admin)

        message.answer.assert_awaited_once()
        assert 'истек' in message.answer.await_args.args[0].lower()
        assert await private.get_state() is None
        assert (await _card(w.db)).status == 'shown'  # карточка свободна: «Править» можно нажать заново
        assert await _admin_messages(w.db) == []
        notify.assert_not_awaited()
        bot.edit_message_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_edit_just_inside_the_window_is_still_sent(monkeypatch, bot, notify):
    async with world(monkeypatch) as w:
        private = _private_state(w, ADMIN_TG)
        await private.set_state(SupportAssistStates.waiting_edit)
        await private.set_data(_waiting(edit_started_at=time.time() - 29 * 60))

        await sa.handle_edit_text(_text_message(OWNER_TEXT, ADMIN_TG), private, bot, w.db, w.admin)

        notify.assert_awaited_once()
        assert (await _card(w.db)).status == 'edited'


@pytest.mark.asyncio
@pytest.mark.parametrize('stamp', [None, 'yesterday', True])
async def test_edit_without_a_usable_start_time_counts_as_expired(monkeypatch, bot, notify, stamp):
    """Не доказать, что запрос свежий, — не отправляем."""
    async with world(monkeypatch) as w:
        private = _private_state(w, ADMIN_TG)
        await private.set_state(SupportAssistStates.waiting_edit)
        data = _waiting()
        data.pop('edit_started_at')
        if stamp is not None:
            data['edit_started_at'] = stamp
        await private.set_data(data)
        message = _text_message(OWNER_TEXT, ADMIN_TG)

        await sa.handle_edit_text(message, private, bot, w.db, w.admin)

        assert 'истек' in message.answer.await_args.args[0].lower()
        assert await private.get_state() is None
        notify.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('command', ['/start', '/cancel', '/admin_help', '  /menu', '/'])
async def test_a_command_is_never_sent_to_the_client(monkeypatch, bot, notify, command):
    async with world(monkeypatch) as w:
        private = _private_state(w, ADMIN_TG)
        await private.set_state(SupportAssistStates.waiting_edit)
        await private.set_data(_waiting())
        message = _text_message(command, ADMIN_TG)

        await sa.handle_edit_text(message, private, bot, w.db, w.admin)

        message.answer.assert_awaited_once()
        assert 'команд' in message.answer.await_args.args[0].lower()
        assert await private.get_state() == SupportAssistStates.waiting_edit.state  # правка ждёт обычного текста
        assert (await _card(w.db)).status == 'shown'
        assert await _admin_messages(w.db) == []
        notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_slash_inside_the_text_is_fine(monkeypatch, bot, notify):
    async with world(monkeypatch) as w:
        private = _private_state(w, ADMIN_TG)
        await private.set_state(SupportAssistStates.waiting_edit)
        await private.set_data(_waiting())
        text = 'Откройте Настройки/Устройства и удалите лишнее.'

        await sa.handle_edit_text(_text_message(text, ADMIN_TG), private, bot, w.db, w.admin)

        (reply,) = await _admin_messages(w.db)
        assert reply.message_text == text


@pytest.mark.asyncio
async def test_cancel_clears_the_waiting_state(monkeypatch, bot, notify):
    async with world(monkeypatch) as w:
        private = _private_state(w, ADMIN_TG)
        await private.set_state(SupportAssistStates.waiting_edit)
        await private.set_data(_waiting())
        cb = _callback('sa:cancel:5', ADMIN_TG, chat_type=ChatType.PRIVATE)

        await sa.handle_cancel(cb, private)

        assert await private.get_state() is None
        assert await private.get_data() == {}
        cb.answer.assert_awaited_once()
        assert (await _card(w.db)).status == 'shown'


@pytest.mark.asyncio
async def test_stale_cancel_does_not_clear_an_edit_that_is_not_its_own(monkeypatch, bot, notify):
    async with world(monkeypatch, second_card=True) as w:
        private = _private_state(w, ADMIN_TG)
        await private.set_state(SupportAssistStates.waiting_edit)
        await private.set_data(_waiting(sa_card_id=6, sa_message_id=SECOND_CARD_MESSAGE))
        cb = _callback('sa:cancel:5', ADMIN_TG, chat_type=ChatType.PRIVATE)

        await sa.handle_cancel(cb, private)

        assert await private.get_state() == SupportAssistStates.waiting_edit.state
        assert (await private.get_data())['sa_card_id'] == 6
        cb.message.edit_text.assert_awaited_once()
        assert 'устарел' in cb.message.edit_text.await_args.args[0].lower()
        cb.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancel_with_nothing_pending_does_not_claim_the_edit_was_cancelled(monkeypatch, bot, notify):
    async with world(monkeypatch) as w:
        private = _private_state(w, ADMIN_TG)
        cb = _callback('sa:cancel:5', ADMIN_TG, chat_type=ChatType.PRIVATE)

        await sa.handle_cancel(cb, private)

        assert await private.get_state() is None
        assert 'устарел' in cb.message.edit_text.await_args.args[0].lower()


@pytest.mark.parametrize('data', ['sa:cancel', 'sa:cancel:5'])
def test_cancel_is_private_only(data):
    assert not is_group_safe_callback(data)


# --- два «Править» подряд: ответ не уходит не тому клиенту ----------------------------------------------------------


@pytest.mark.asyncio
async def test_second_edit_retires_the_first_prompt_and_takes_over_the_dialog(monkeypatch, bot, notify):
    async with world(monkeypatch, second_card=True) as w:
        bot.send_message.side_effect = [SimpleNamespace(message_id=900), SimpleNamespace(message_id=901)]
        group = _group_state(w, ADMIN_TG)

        await sa.handle_edit(_callback('sa:edit:5', ADMIN_TG), group, bot, w.db)
        bot.edit_message_text.assert_not_awaited()  # первый запрос ничего не заменяет
        await sa.handle_edit(_callback('sa:edit:6', ADMIN_TG, message_id=SECOND_CARD_MESSAGE), group, bot, w.db)

        # старый запрос (900) переписан: ни кнопки «Отмена», ни вида «ещё жду текст»
        bot.edit_message_text.assert_awaited_once()
        old = bot.edit_message_text.await_args.kwargs
        assert (old['chat_id'], old['message_id']) == (ADMIN_TG, 900)
        assert '#2' in old['text'] and 'замен' in old['text'].lower()
        assert _has_no_buttons(old['reply_markup'])
        # диалог теперь про карточку 6, и именно его сообщение править на строку решения
        private = _private_state(w, ADMIN_TG)
        assert await private.get_state() == SupportAssistStates.waiting_edit.state
        data = await private.get_data()
        assert isinstance(data.pop('edit_started_at'), float)
        assert data == {
            'sa_card_id': 6,
            'sa_chat_id': GROUP_CHAT,
            'sa_message_id': SECOND_CARD_MESSAGE,
            'sa_prompt_id': 901,
        }
        notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_batching_two_edits_answers_only_the_last_one_and_never_the_wrong_client(monkeypatch, bot, notify):
    """Сценарий ревью: «Править» на двух карточках, затем текст, написанный для первой."""
    async with world(monkeypatch, second_card=True) as w:
        bot.send_message.side_effect = [SimpleNamespace(message_id=900), SimpleNamespace(message_id=901)]
        group = _group_state(w, ADMIN_TG)
        await sa.handle_edit(_callback('sa:edit:5', ADMIN_TG), group, bot, w.db)
        await sa.handle_edit(_callback('sa:edit:6', ADMIN_TG, message_id=SECOND_CARD_MESSAGE), group, bot, w.db)
        private = _private_state(w, ADMIN_TG)

        # Отмена под устаревшим запросом первой карточки не трогает вторую правку
        stale_cancel = _callback('sa:cancel:5', ADMIN_TG, chat_type=ChatType.PRIVATE, message_id=900)
        await sa.handle_cancel(stale_cancel, private)
        assert (await private.get_data())['sa_card_id'] == 6

        # ответом на устаревший запрос (900) текст не принимается: он мог быть написан для первого клиента
        meant_for_first = _text_message(OWNER_TEXT, ADMIN_TG, reply_to=900)
        await sa.handle_edit_text(meant_for_first, private, bot, w.db, w.admin)

        meant_for_first.answer.assert_awaited_once()
        assert await private.get_state() == SupportAssistStates.waiting_edit.state  # ждём дальше, ничего не ушло
        assert await _admin_messages(w.db) == []
        notify.assert_not_awaited()
        assert (await _card(w.db, 5)).status == 'shown' and (await _card(w.db, 6)).status == 'shown'


@pytest.mark.asyncio
async def test_second_edit_text_goes_only_to_the_second_ticket_and_names_it(monkeypatch, bot, notify):
    async with world(monkeypatch, second_card=True) as w:
        bot.send_message.side_effect = [SimpleNamespace(message_id=900), SimpleNamespace(message_id=901)]
        group = _group_state(w, ADMIN_TG)
        await sa.handle_edit(_callback('sa:edit:5', ADMIN_TG), group, bot, w.db)
        await sa.handle_edit(_callback('sa:edit:6', ADMIN_TG, message_id=SECOND_CARD_MESSAGE), group, bot, w.db)
        private = _private_state(w, ADMIN_TG)

        message = _text_message(OWNER_TEXT, ADMIN_TG, reply_to=901)  # ответом на актуальный запрос — можно
        await sa.handle_edit_text(message, private, bot, w.db, w.admin)

        assert await _admin_messages(w.db, ticket_id=1) == []
        (reply,) = await _admin_messages(w.db, ticket_id=2)
        assert reply.message_text == OWNER_TEXT
        notify.assert_awaited_once()
        assert notify.await_args.args[1].id == 2
        assert (await _card(w.db, 5)).status == 'shown'
        assert (await _card(w.db, 6)).status == 'edited'
        assert '#2' in message.answer.await_args.args[0]  # админ видит, какому тикету ушёл ответ
        assert await private.get_state() is None


@pytest.mark.asyncio
async def test_edit_text_replying_to_a_foreign_message_is_refused_and_keeps_waiting(monkeypatch, bot, notify):
    async with world(monkeypatch) as w:
        private = _private_state(w, ADMIN_TG)
        await private.set_state(SupportAssistStates.waiting_edit)
        await private.set_data(_waiting(sa_prompt_id=900))
        message = _text_message(OWNER_TEXT, ADMIN_TG, reply_to=123)

        await sa.handle_edit_text(message, private, bot, w.db, w.admin)

        assert await private.get_state() == SupportAssistStates.waiting_edit.state
        assert await _admin_messages(w.db) == []
        notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_edit_text_without_a_stored_prompt_id_still_works(monkeypatch, bot, notify):
    """Состояние, поставленное до выкладки этой правки, ключа запроса не знает: не блокируем его."""
    async with world(monkeypatch) as w:
        private = _private_state(w, ADMIN_TG)
        await private.set_state(SupportAssistStates.waiting_edit)
        await private.set_data(_waiting())

        await sa.handle_edit_text(_text_message(OWNER_TEXT, ADMIN_TG, reply_to=123), private, bot, w.db, w.admin)

        notify.assert_awaited_once()


@pytest.mark.asyncio
async def test_repeated_edit_of_the_same_card_retires_the_old_prompt(monkeypatch, bot, notify):
    async with world(monkeypatch) as w:
        bot.send_message.side_effect = [SimpleNamespace(message_id=900), SimpleNamespace(message_id=901)]
        group = _group_state(w, ADMIN_TG)

        await sa.handle_edit(_callback('sa:edit:5', ADMIN_TG), group, bot, w.db)
        await sa.handle_edit(_callback('sa:edit:5', ADMIN_TG), group, bot, w.db)

        assert bot.edit_message_text.await_args.kwargs['message_id'] == 900
        assert (await _private_state(w, ADMIN_TG).get_data())['sa_prompt_id'] == 901


@pytest.mark.asyncio
async def test_second_edit_with_closed_dm_leaves_the_first_dialog_untouched(monkeypatch, bot, notify):
    async with world(monkeypatch, second_card=True) as w:
        group = _group_state(w, ADMIN_TG)
        await sa.handle_edit(_callback('sa:edit:5', ADMIN_TG), group, bot, w.db)
        before = await _private_state(w, ADMIN_TG).get_data()
        bot.send_message.side_effect = TelegramForbiddenError(method=MagicMock(), message='Forbidden')

        await sa.handle_edit(_callback('sa:edit:6', ADMIN_TG, message_id=SECOND_CARD_MESSAGE), group, bot, w.db)

        assert await _private_state(w, ADMIN_TG).get_data() == before
        bot.edit_message_text.assert_not_awaited()  # первый запрос остаётся в силе


@pytest.mark.asyncio
async def test_failure_to_retire_the_old_prompt_does_not_block_the_new_edit(monkeypatch, bot, notify):
    async with world(monkeypatch, second_card=True) as w:
        bot.send_message.side_effect = [SimpleNamespace(message_id=900), SimpleNamespace(message_id=901)]
        bot.edit_message_text.side_effect = TelegramBadRequest(method=MagicMock(), message='message to edit not found')
        group = _group_state(w, ADMIN_TG)
        await sa.handle_edit(_callback('sa:edit:5', ADMIN_TG), group, bot, w.db)

        await sa.handle_edit(_callback('sa:edit:6', ADMIN_TG, message_id=SECOND_CARD_MESSAGE), group, bot, w.db)

        assert (await _private_state(w, ADMIN_TG).get_data())['sa_card_id'] == 6


# --- пропустить ----------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_skip_dismisses_the_card_and_sends_nothing(monkeypatch, bot, notify):
    async with world(monkeypatch) as w:
        await sa.handle_skip(_callback('sa:skip:5', ADMIN_TG), bot, w.db)

        row = await _card(w.db)
        assert (row.status, row.decided_by, row.final_text) == ('dismissed', ADMIN_TG, None)
        notify.assert_not_awaited()
        bot.send_message.assert_not_awaited()
        assert await _admin_messages(w.db) == []
        edit = _card_message_edit(bot)
        assert (edit['chat_id'], edit['message_id']) == (GROUP_CHAT, CARD_MESSAGE)
        assert edit['text'].endswith('⏭ пропущено')
        assert _has_no_buttons(edit['reply_markup'])


@pytest.mark.asyncio
async def test_skip_works_on_a_card_without_the_model(monkeypatch, bot, notify):
    """У карточки без модели статус 'fallback' и единственная кнопка «Пропустить»."""
    async with world(monkeypatch, card_status='fallback', card=None) as w:
        cb = _callback('sa:skip:5', ADMIN_TG)

        await sa.handle_skip(cb, bot, w.db)

        assert (await _card(w.db)).status == 'dismissed'
        assert 'уже обработано' not in (_alert(cb)[0] or '').lower()
        assert _card_message_edit(bot)['text'].endswith('⏭ пропущено')


@pytest.mark.asyncio
async def test_skip_on_a_decided_card_changes_nothing(monkeypatch, bot, notify):
    async with world(monkeypatch, card_status='sent') as w:
        cb = _callback('sa:skip:5', ADMIN_TG)

        await sa.handle_skip(cb, bot, w.db)

        assert (await _card(w.db)).status == 'sent'
        assert 'уже обработано' in _alert(cb)[0].lower()
        bot.edit_message_text.assert_not_awaited()


# --- общий путь с ручным ответом ------------------------------------------------------------------------------------


def test_card_buttons_and_manual_reply_share_one_send_function():
    assert sa.send_admin_reply is admin_tickets.send_admin_reply
    manual = inspect.getsource(admin_tickets.handle_admin_ticket_reply)
    assert 'send_admin_reply' in manual
    assert 'add_message' not in manual  # последовательность живёт в одной функции, а не копируется


@pytest.mark.asyncio
async def test_send_admin_reply_adds_the_admin_message_then_notifies(monkeypatch, bot, notify):
    async with world(monkeypatch) as w:
        ticket = await admin_tickets.TicketCRUD.get_ticket_by_id(w.db, 1, load_messages=False, load_user=True)
        order: list[str] = []
        real_add = admin_tickets.TicketMessageCRUD.add_message

        async def add(*args, **kwargs):
            order.append('add')
            return await real_add(*args, **kwargs)

        monkeypatch.setattr(admin_tickets.TicketMessageCRUD, 'add_message', add)
        notify.side_effect = lambda *a, **k: order.append('notify')

        stored = await admin_tickets.send_admin_reply(
            bot, w.db, ticket, 2, 'Ответ', media_type='photo', media_file_id='f'
        )

        assert order == ['add', 'notify']
        assert (stored.message_text, stored.is_from_admin, stored.media_type) == ('Ответ', True, 'photo')
        assert notify.await_args.args[2] == 'Ответ'


# --- регистрация ---------------------------------------------------------------------------------------------------


def test_handlers_are_registered_on_the_dispatcher():
    dp = Dispatcher()

    sa.register_handlers(dp)

    callbacks = {h.callback.__name__ for h in dp.callback_query.handlers}
    messages = {h.callback.__name__ for h in dp.message.handlers}
    assert {'handle_send', 'handle_edit', 'handle_skip', 'handle_cancel'} <= callbacks
    assert 'handle_edit_text' in messages


def test_bot_wires_the_handlers_next_to_the_ticket_ones():
    source = (Path(__file__).resolve().parents[2] / 'app' / 'bot.py').read_text(encoding='utf-8')

    assert 'admin_support_assist.register_handlers(dp)' in source
