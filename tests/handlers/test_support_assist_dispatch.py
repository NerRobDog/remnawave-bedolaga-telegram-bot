"""Кнопки карточки через настоящий диспетчер aiogram.

Юнит-тесты зовут обработчики напрямую и не видят трёх вещей, на которых держится «Править» в группе:
внедрение аргументов (bot, state, db, db_user), фильтр ``ChatTypeFilterMiddleware`` и совпадение ключа
личного FSM, который ставит обработчик, с ключом, по которому диспетчер ищет состояние при следующем
сообщении админа в личке. Здесь Update проходит весь путь; сеть заменена записью вызовов методов бота.
"""

from __future__ import annotations

import itertools
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.enums import ChatType
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, TelegramObject, Update, User as TgUser

from app.handlers.admin import support_assist as sa
from app.middlewares.chat_type_filter import ChatTypeFilterMiddleware
from tests.handlers.test_support_assist_buttons import (  # noqa: F401  (autouse-фикстура env)
    ADMIN_TG,
    CARD_MESSAGE,
    GROUP_CHAT,
    OWNER_TEXT,
    SECOND_CARD_MESSAGE,
    TOPIC,
    _admin_messages,
    _card,
    env,
    notify,
    world,
)


class _InjectDb(BaseMiddleware):
    """Вместо AuthMiddleware: кладёт сессию и пользователя БД, как это делает он."""

    def __init__(self, db, users: dict[int, Any]):
        self.db = db
        self.users = users

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        data['db'] = self.db
        data['db_user'] = self.users.get(event.from_user.id)
        return await handler(event, data)


def _tg_user(user_id: int) -> TgUser:
    return TgUser(id=user_id, is_bot=False, first_name='Админ')


def _press(data: str, user_id: int, update_id: int = 1, message_id: int = CARD_MESSAGE) -> Update:
    card_message = Message(
        message_id=message_id,
        date=datetime.now(UTC),
        chat=Chat(id=GROUP_CHAT, type=ChatType.SUPERGROUP),
        message_thread_id=TOPIC,
        is_topic_message=True,
    )
    callback = CallbackQuery(
        id=str(update_id), from_user=_tg_user(user_id), chat_instance='x', data=data, message=card_message
    )
    return Update(update_id=update_id, callback_query=callback)


def _private_press(data: str, user_id: int, message_id: int, update_id: int = 1) -> Update:
    """Нажатие кнопки под запросом правки в личке админа."""
    prompt = Message(message_id=message_id, date=datetime.now(UTC), chat=Chat(id=user_id, type=ChatType.PRIVATE))
    callback = CallbackQuery(
        id=str(update_id), from_user=_tg_user(user_id), chat_instance='x', data=data, message=prompt
    )
    return Update(update_id=update_id, callback_query=callback)


def _private_text(text: str, user_id: int, update_id: int = 2, reply_to: int | None = None) -> Update:
    replied = (
        Message(message_id=reply_to, date=datetime.now(UTC), chat=Chat(id=user_id, type=ChatType.PRIVATE))
        if reply_to is not None
        else None
    )
    message = Message(
        message_id=9,
        date=datetime.now(UTC),
        chat=Chat(id=user_id, type=ChatType.PRIVATE),
        from_user=_tg_user(user_id),
        text=text,
        reply_to_message=replied,
    )
    return Update(update_id=update_id, message=message)


@pytest.fixture
def telegram(monkeypatch):
    """Бот с настоящим токеном-заглушкой; методы не уходят в сеть, а пишутся в список."""
    calls: list[Any] = []
    message_ids = itertools.count(900)

    async def fake_call(self, method, request_timeout=None):
        calls.append(method)
        if isinstance(method, SendMessage):
            return Message(
                message_id=next(message_ids),
                date=datetime.now(UTC),
                chat=Chat(id=method.chat_id, type=ChatType.PRIVATE),
            )
        return True

    monkeypatch.setattr(Bot, '__call__', fake_call)
    return SimpleNamespace(bot=Bot(token='42:TESTTOKEN'), calls=calls)


def _dispatcher(w) -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    users = {ADMIN_TG: w.admin}
    chat_filter = ChatTypeFilterMiddleware()
    inject = _InjectDb(w.db, users)
    for observer in (dp.message, dp.callback_query):
        observer.middleware(chat_filter)
        observer.middleware(inject)
    sa.register_handlers(dp)
    return dp


def _of(calls: list[Any], kind: type) -> list[Any]:
    return [c for c in calls if isinstance(c, kind)]


@pytest.mark.asyncio
async def test_edit_in_a_group_topic_continues_with_the_next_private_message(monkeypatch, telegram, notify):
    async with world(monkeypatch) as w:
        dp = _dispatcher(w)

        await dp.feed_update(telegram.bot, _press('sa:edit:5', ADMIN_TG))

        (dm,) = _of(telegram.calls, SendMessage)
        assert dm.chat_id == ADMIN_TG
        (answer,) = _of(telegram.calls, AnswerCallbackQuery)
        assert 'личные сообщения' in answer.text
        assert (await _card(w.db)).status == 'shown'
        telegram.calls.clear()

        # то же состояние диспетчер находит по обычному ключу личного чата
        await dp.feed_update(telegram.bot, _private_text(OWNER_TEXT, ADMIN_TG))

        notify.assert_awaited_once()
        assert notify.await_args.args[2] == OWNER_TEXT
        (reply,) = await _admin_messages(w.db)
        assert (reply.message_text, reply.user_id) == (OWNER_TEXT, 2)
        row = await _card(w.db)
        assert (row.status, row.final_text, row.decided_by) == ('edited', OWNER_TEXT, ADMIN_TG)
        (edit,) = _of(telegram.calls, EditMessageText)
        assert (edit.chat_id, edit.message_id) == (GROUP_CHAT, CARD_MESSAGE)
        assert edit.text.endswith('✅ отправлено (текст изменён)')
        telegram.calls.clear()

        # состояние снято: следующий текст админа в личке уже не уходит клиенту
        await dp.feed_update(telegram.bot, _private_text('просто заметка', ADMIN_TG, update_id=3))
        notify.assert_awaited_once()


@pytest.mark.asyncio
async def test_send_and_skip_run_from_the_group(monkeypatch, telegram, notify):
    async with world(monkeypatch) as w:
        dp = _dispatcher(w)

        await dp.feed_update(telegram.bot, _press('sa:send:5', ADMIN_TG))

        notify.assert_awaited_once()
        assert (await _card(w.db)).status == 'sent'
        (edit,) = _of(telegram.calls, EditMessageText)
        assert edit.text.endswith('✅ отправлено')

    async with world(monkeypatch) as w:
        telegram.calls.clear()
        dp = _dispatcher(w)

        await dp.feed_update(telegram.bot, _press('sa:skip:5', ADMIN_TG))

        assert (await _card(w.db)).status == 'dismissed'
        assert _of(telegram.calls, EditMessageText)[0].text.endswith('⏭ пропущено')


@pytest.mark.asyncio
async def test_a_group_does_not_reach_the_private_only_cancel(monkeypatch, telegram, notify):
    async with world(monkeypatch) as w:
        dp = _dispatcher(w)

        await dp.feed_update(telegram.bot, _press('sa:cancel:5', ADMIN_TG))

        assert telegram.calls == []


@pytest.mark.asyncio
async def test_cancel_under_the_private_prompt_ends_the_edit(monkeypatch, telegram, notify):
    async with world(monkeypatch) as w:
        dp = _dispatcher(w)
        await dp.feed_update(telegram.bot, _press('sa:edit:5', ADMIN_TG))
        (dm,) = _of(telegram.calls, SendMessage)
        assert dm.reply_markup.inline_keyboard[0][0].callback_data == 'sa:cancel:5'
        telegram.calls.clear()

        await dp.feed_update(telegram.bot, _private_press('sa:cancel:5', ADMIN_TG, message_id=900, update_id=2))
        await dp.feed_update(telegram.bot, _private_text(OWNER_TEXT, ADMIN_TG, update_id=3))

        notify.assert_not_awaited()
        assert await _admin_messages(w.db) == []
        assert (await _card(w.db)).status == 'shown'


@pytest.mark.asyncio
async def test_edit_on_two_cards_in_a_row_sends_to_the_last_one_only(monkeypatch, telegram, notify):
    async with world(monkeypatch, second_card=True) as w:
        dp = _dispatcher(w)
        await dp.feed_update(telegram.bot, _press('sa:edit:5', ADMIN_TG))
        await dp.feed_update(telegram.bot, _press('sa:edit:6', ADMIN_TG, update_id=2, message_id=SECOND_CARD_MESSAGE))
        (old,) = _of(telegram.calls, EditMessageText)  # первый запрос (900) переписан на «заменён»
        assert (old.chat_id, old.message_id) == (ADMIN_TG, 900)
        telegram.calls.clear()

        # «Отмена» старого запроса не гасит правку второй карточки
        await dp.feed_update(telegram.bot, _private_press('sa:cancel:5', ADMIN_TG, message_id=900, update_id=3))
        # текст ответом на старый запрос не уходит никому
        await dp.feed_update(telegram.bot, _private_text(OWNER_TEXT, ADMIN_TG, update_id=4, reply_to=900))
        notify.assert_not_awaited()
        assert await _admin_messages(w.db) == []

        # обычный текст уходит клиенту второго тикета
        await dp.feed_update(telegram.bot, _private_text(OWNER_TEXT, ADMIN_TG, update_id=5))
        notify.assert_awaited_once()
        assert notify.await_args.args[1].id == 2
        assert await _admin_messages(w.db, ticket_id=1) == []
        assert (await _card(w.db, 5)).status == 'shown'
        assert (await _card(w.db, 6)).status == 'edited'
