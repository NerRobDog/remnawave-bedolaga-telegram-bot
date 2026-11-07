"""Обязательная анкета перед тикетом: валидация, показ владельцу, кабинет, бот."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.cabinet.dependencies import get_cabinet_db, get_current_cabinet_user
from app.cabinet.routes import tickets as cabinet_tickets
from app.config import settings
from app.database.models import Ticket, TicketMessage, User
from app.handlers import tickets as bot_tickets
from app.localization.texts import get_texts
from app.services.event_emitter import event_emitter
from app.services.support_assist.ticket_form import (
    FORM_FIELDS,
    OTHER_MAX_LENGTH,
    render_diagnostics,
    validate_diagnostics,
)
from tests.fixtures.sqlite_memory import memory_session


FULL = {
    'platform': 'ios',
    'app': 'happ',
    'network': 'wifi',
    'symptom': 'nothing',
    'since': 'today',
}

LOCALE_DIR = Path(__file__).resolve().parents[3] / 'app' / 'localization' / 'locales'
LANGS = ('ru', 'en', 'ua', 'fa', 'zh')


# --- модель анкеты -----------------------------------------------------------


def test_form_fields_match_the_spec():
    assert FORM_FIELDS == {
        'platform': ('ios', 'android', 'windows', 'macos', 'router', 'other'),
        'app': ('clash_mi', 'happ', 'rabbit_hole', 'flclash_x', 'other'),
        'network': ('mobile', 'wifi', 'both'),
        'symptom': ('nothing', 'specific_site', 'slow', 'no_connect'),
        'since': ('today', 'days', 'always', 'after_update'),
    }
    assert list(FORM_FIELDS) == ['platform', 'app', 'network', 'symptom', 'since']


def test_validate_returns_full_answers_as_is():
    assert validate_diagnostics(dict(FULL)) == FULL


@pytest.mark.parametrize('field', list(FORM_FIELDS))
def test_validate_rejects_a_missing_field(field):
    raw = {k: v for k, v in FULL.items() if k != field}
    with pytest.raises(ValueError, match=field):
        validate_diagnostics(raw)


@pytest.mark.parametrize('field', list(FORM_FIELDS))
def test_validate_rejects_an_empty_field(field):
    with pytest.raises(ValueError, match=field):
        validate_diagnostics({**FULL, field: ''})


@pytest.mark.parametrize('raw', [None, {}, [], 'ios', 5])
def test_validate_rejects_a_missing_or_malformed_form(raw):
    with pytest.raises(ValueError):
        validate_diagnostics(raw)


@pytest.mark.parametrize('field', list(FORM_FIELDS))
def test_validate_rejects_an_unknown_option(field):
    with pytest.raises(ValueError, match=field):
        validate_diagnostics({**FULL, field: 'no_such_option'})


@pytest.mark.parametrize('value', [None, 1, ['ios'], {'ios': 1}])
def test_validate_rejects_a_non_string_option(value):
    with pytest.raises(ValueError):
        validate_diagnostics({**FULL, 'platform': value})


def test_validate_truncates_other_text_to_100_chars():
    out = validate_diagnostics({**FULL, 'platform': 'other', 'platform_other': 'x' * 300})
    assert out['platform_other'] == 'x' * OTHER_MAX_LENGTH
    assert OTHER_MAX_LENGTH == 100


def test_validate_strips_other_text_and_collapses_whitespace():
    out = validate_diagnostics({**FULL, 'app': 'other', 'app_other': '  Steam\n  Deck \t'})
    assert out['app_other'] == 'Steam Deck'


@pytest.mark.parametrize('text', ['', '   ', '\n\t ', None])
def test_validate_rejects_other_without_a_text(text):
    """«Другое» без текста бесполезно ассистенту; бот такое не пропускает, и бэкенд обязан то же."""
    with pytest.raises(ValueError, match='app_other'):
        validate_diagnostics({**FULL, 'app': 'other', 'app_other': text})


def test_validate_rejects_other_when_the_text_key_is_missing():
    with pytest.raises(ValueError, match='platform_other'):
        validate_diagnostics({**FULL, 'platform': 'other'})


def test_validate_rejects_non_string_other_text():
    with pytest.raises(ValueError, match='platform_other'):
        validate_diagnostics({**FULL, 'platform': 'other', 'platform_other': 12})


def test_validate_ignores_other_text_when_other_was_not_chosen():
    out = validate_diagnostics({**FULL, 'platform_other': 'stale text'})
    assert out == FULL


def test_validate_drops_unknown_keys():
    assert validate_diagnostics({**FULL, 'telegram_id': 1, 'email': 'a@b.c'}) == FULL


def test_validate_does_not_mutate_its_input():
    raw = {**FULL, 'platform': 'other', 'platform_other': '  x  '}
    snapshot = dict(raw)
    validate_diagnostics(raw)
    assert raw == snapshot


# --- показ владельцу ---------------------------------------------------------


def test_render_russian_lists_every_answer_with_readable_labels():
    text = render_diagnostics(FULL, 'ru')
    lines = text.splitlines()
    assert len(lines) == 5
    assert all(': ' in line for line in lines)
    assert 'Happ' in text
    assert 'ios' not in text and 'wifi' not in text  # id вариантов наружу не идут


def test_render_english_differs_from_russian():
    assert render_diagnostics(FULL, 'en') != render_diagnostics(FULL, 'ru')
    assert 'Wi-Fi' in render_diagnostics(FULL, 'en')


def test_render_shows_the_other_text():
    d = {**FULL, 'platform': 'other', 'platform_other': 'Steam Deck'}
    assert 'Steam Deck' in render_diagnostics(d, 'ru')


def test_render_is_plain_text_the_caller_escapes():
    d = {**FULL, 'platform': 'other', 'platform_other': '<b>x</b>'}
    assert '<b>x</b>' in render_diagnostics(d, 'ru')


def test_render_skips_garbage_and_handles_empty():
    assert render_diagnostics({}, 'ru') == ''
    assert render_diagnostics(None, 'ru') == ''  # type: ignore[arg-type]
    assert render_diagnostics({'platform': 'nope'}, 'ru') == ''


@pytest.mark.parametrize('lang', LANGS)
def test_every_form_text_exists_in_every_locale(lang):
    values = json.loads((LOCALE_DIR / f'{lang}.json').read_text(encoding='utf-8'))
    expected = {
        'TICKET_FORM_TITLE',
        'TICKET_FORM_STEP',
        'TICKET_FORM_OTHER_PROMPT',
        'TICKET_FORM_OTHER_INVALID',
        'TICKET_FORM_RESTART',
    }
    for field, options in FORM_FIELDS.items():
        expected |= {f'TICKET_FORM_{field.upper()}_PROMPT', f'TICKET_FORM_{field.upper()}_LABEL'}
        expected |= {f'TICKET_FORM_{field.upper()}_{option.upper()}' for option in options}
    missing = sorted(k for k in expected if not str(values.get(k, '')).strip())
    assert not missing, f'{lang}: {missing}'


def test_telegram_callback_data_of_every_option_fits_64_bytes():
    for field, options in FORM_FIELDS.items():
        for option in options:
            assert len(f'tf:{field}:{option}'.encode()) <= 64


# --- кабинет -----------------------------------------------------------------


@pytest.fixture
def _flag(monkeypatch):
    def set_flag(value: bool) -> None:
        monkeypatch.setattr(settings, 'SUPPORT_TICKET_FORM_REQUIRED', value)

    return set_flag


async def _post(monkeypatch, body: dict):
    """Один POST /cabinet/tickets против настоящей SQLite-сессии; возвращает ответ и сохранённый тикет."""
    monkeypatch.setattr(cabinet_tickets.SupportSettingsService, 'is_tickets_enabled', lambda: True)
    monkeypatch.setattr(event_emitter, 'emit', AsyncMock())
    monkeypatch.setattr(cabinet_tickets, 'notify_admins_about_new_ticket', AsyncMock())
    monkeypatch.setattr(
        cabinet_tickets.TicketNotificationCRUD, 'create_admin_notification_for_new_ticket', AsyncMock(return_value=None)
    )
    async with memory_session(monkeypatch, [User.__table__, Ticket.__table__, TicketMessage.__table__]) as db:
        owner = User(id=1, telegram_id=1, language='ru')
        db.add(owner)
        await db.commit()

        async def request_db():
            yield db

        app = FastAPI()
        app.include_router(cabinet_tickets.router, prefix='/cabinet')
        app.dependency_overrides[get_cabinet_db] = request_db
        app.dependency_overrides[get_current_cabinet_user] = lambda: owner
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://cabinet') as client:
            response = await client.post('/cabinet/tickets', json=body)
        db.expire_all()
        stored = (await db.execute(select(Ticket))).scalars().all()
        return response, stored


BASE_BODY = {'title': 'Не работает VPN', 'message': 'Помогите, пожалуйста'}


@pytest.mark.asyncio
async def test_cabinet_flag_on_without_diagnostics_is_422(monkeypatch, _flag):
    _flag(True)
    response, stored = await _post(monkeypatch, dict(BASE_BODY))
    assert response.status_code == 422
    assert 'diagnostics' in json.dumps(response.json()).lower()
    assert stored == []  # тикет не создан


@pytest.mark.asyncio
async def test_cabinet_flag_on_with_incomplete_diagnostics_is_422(monkeypatch, _flag):
    _flag(True)
    partial = {k: v for k, v in FULL.items() if k != 'since'}
    response, stored = await _post(monkeypatch, {**BASE_BODY, 'diagnostics': partial})
    assert response.status_code == 422
    assert stored == []


@pytest.mark.asyncio
async def test_cabinet_flag_on_with_unknown_option_is_422(monkeypatch, _flag):
    _flag(True)
    response, stored = await _post(monkeypatch, {**BASE_BODY, 'diagnostics': {**FULL, 'network': 'satellite'}})
    assert response.status_code == 422
    assert stored == []


@pytest.mark.asyncio
@pytest.mark.parametrize('extra', [{}, {'platform_other': '   '}])
async def test_cabinet_flag_on_other_without_text_is_422(monkeypatch, _flag, extra):
    _flag(True)
    response, stored = await _post(monkeypatch, {**BASE_BODY, 'diagnostics': {**FULL, 'platform': 'other', **extra}})
    assert response.status_code == 422
    assert stored == []


@pytest.mark.asyncio
async def test_cabinet_flag_off_other_without_text_drops_diagnostics_but_creates(monkeypatch, _flag):
    _flag(False)
    response, stored = await _post(monkeypatch, {**BASE_BODY, 'diagnostics': {**FULL, 'platform': 'other'}})
    assert response.status_code == 200, response.text
    assert stored[0].diagnostics is None


@pytest.mark.asyncio
async def test_cabinet_flag_on_with_diagnostics_stores_them(monkeypatch, _flag):
    _flag(True)
    body = {**BASE_BODY, 'diagnostics': {**FULL, 'platform': 'other', 'platform_other': ' Steam Deck '}}
    response, stored = await _post(monkeypatch, body)
    assert response.status_code == 200, response.text
    assert len(stored) == 1
    assert stored[0].diagnostics == {**FULL, 'platform': 'other', 'platform_other': 'Steam Deck'}


@pytest.mark.asyncio
async def test_cabinet_flag_off_without_diagnostics_creates_as_before(monkeypatch, _flag):
    _flag(False)
    response, stored = await _post(monkeypatch, dict(BASE_BODY))
    assert response.status_code == 200, response.text
    assert len(stored) == 1
    assert stored[0].diagnostics is None


@pytest.mark.asyncio
async def test_cabinet_flag_off_keeps_valid_diagnostics(monkeypatch, _flag):
    _flag(False)
    response, stored = await _post(monkeypatch, {**BASE_BODY, 'diagnostics': dict(FULL)})
    assert response.status_code == 200, response.text
    assert stored[0].diagnostics == FULL


@pytest.mark.asyncio
async def test_cabinet_flag_off_drops_broken_diagnostics_but_still_creates(monkeypatch, _flag):
    _flag(False)
    response, stored = await _post(monkeypatch, {**BASE_BODY, 'diagnostics': {'platform': 'ios'}})
    assert response.status_code == 200, response.text
    assert stored[0].diagnostics is None


def test_flag_defaults_to_off():
    from app.config import Settings

    assert Settings.model_fields['SUPPORT_TICKET_FORM_REQUIRED'].default is False


# --- бот ---------------------------------------------------------------------


def _state() -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=10, user_id=10))


def _callback(data: str) -> SimpleNamespace:
    message = SimpleNamespace(
        chat=SimpleNamespace(id=10),
        message_id=77,
        edit_text=AsyncMock(),
        answer=AsyncMock(return_value=SimpleNamespace(chat=SimpleNamespace(id=10), message_id=78)),
        delete=AsyncMock(),
    )
    return SimpleNamespace(data=data, message=message, answer=AsyncMock(), from_user=SimpleNamespace(id=10))


def _buttons(callback: SimpleNamespace) -> list[str]:
    """callback_data кнопок последнего показанного экрана."""
    call = callback.message.edit_text.await_args
    markup = call.kwargs.get('reply_markup') if call else None
    assert markup is not None, 'экран показан без клавиатуры'
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def _shown_text(callback: SimpleNamespace) -> str:
    return callback.message.edit_text.await_args.args[0]


@pytest.fixture
def bot_env(monkeypatch):
    user = SimpleNamespace(id=5, language='ru')
    monkeypatch.setattr(bot_tickets.TicketCRUD, 'is_user_globally_blocked', AsyncMock(return_value=None))
    monkeypatch.setattr(bot_tickets.TicketCRUD, 'user_has_active_ticket', AsyncMock(return_value=False))
    monkeypatch.setattr(bot_tickets, '_try_delete_message_later', AsyncMock())
    return user


async def _start(bot_env, state, flag: bool, monkeypatch) -> SimpleNamespace:
    monkeypatch.setattr(settings, 'SUPPORT_TICKET_FORM_REQUIRED', flag)
    callback = _callback('create_ticket')
    await bot_tickets.show_ticket_priority_selection(callback, state, bot_env, MagicMock())
    return callback


@pytest.mark.asyncio
async def test_bot_flag_on_first_step_asks_for_the_platform(bot_env, monkeypatch):
    state = _state()
    callback = await _start(bot_env, state, True, monkeypatch)
    assert await state.get_state() == bot_tickets.TicketStates.waiting_for_form
    buttons = _buttons(callback)
    assert [b for b in buttons if b.startswith('tf:')] == [f'tf:platform:{o}' for o in FORM_FIELDS['platform']]
    assert 'cancel_ticket_creation' in buttons  # из анкеты можно выйти
    assert get_texts('ru').t('TICKET_FORM_PLATFORM_PROMPT') in _shown_text(callback)
    assert (await state.get_data())['prompt_message_id'] == 77


@pytest.mark.asyncio
async def test_bot_flag_off_keeps_the_old_title_step(bot_env, monkeypatch):
    state = _state()
    callback = await _start(bot_env, state, False, monkeypatch)
    assert await state.get_state() == bot_tickets.TicketStates.waiting_for_title
    assert _shown_text(callback) == get_texts('ru').t('TICKET_TITLE_INPUT', 'Введите заголовок тикета:')
    assert not any(b.startswith('tf:') for b in _buttons(callback))


async def _answer(state, bot_env, field: str, option: str) -> SimpleNamespace:
    callback = _callback(f'tf:{field}:{option}')
    await bot_tickets.handle_ticket_form_answer(callback, state, bot_env)
    return callback


@pytest.mark.asyncio
async def test_bot_walks_all_five_questions_then_asks_for_the_title(bot_env, monkeypatch):
    state = _state()
    await _start(bot_env, state, True, monkeypatch)
    last = None
    for field, option in FULL.items():
        assert (await state.get_state()) == bot_tickets.TicketStates.waiting_for_form
        last = await _answer(state, bot_env, field, option)
    assert await state.get_state() == bot_tickets.TicketStates.waiting_for_title
    data = await state.get_data()
    assert data['diagnostics'] == FULL
    assert _shown_text(last) == get_texts('ru').t('TICKET_TITLE_INPUT', 'Введите заголовок тикета:')


@pytest.mark.asyncio
async def test_bot_each_step_asks_the_next_field(bot_env, monkeypatch):
    state = _state()
    await _start(bot_env, state, True, monkeypatch)
    order = list(FORM_FIELDS)
    for index, (field, option) in enumerate(list(FULL.items())[:-1]):
        callback = await _answer(state, bot_env, field, option)
        nxt = order[index + 1]
        assert f'tf:{nxt}:{FORM_FIELDS[nxt][0]}' in _buttons(callback)


@pytest.mark.asyncio
async def test_bot_resends_the_question_when_the_message_cannot_be_edited(bot_env, monkeypatch):
    """Под кнопкой было фото или сообщение не редактируется: вопрос уходит новым сообщением, и дальше правим его."""
    from aiogram.exceptions import TelegramBadRequest

    monkeypatch.setattr(settings, 'SUPPORT_TICKET_FORM_REQUIRED', True)
    state = _state()
    callback = _callback('create_ticket')
    callback.message.edit_text = AsyncMock(side_effect=TelegramBadRequest(method=MagicMock(), message='no text'))
    await bot_tickets.show_ticket_priority_selection(callback, state, bot_env, MagicMock())
    callback.message.delete.assert_awaited()
    callback.message.answer.assert_awaited_once()
    assert (await state.get_data())['prompt_message_id'] == 78  # id нового сообщения
    assert await state.get_state() == bot_tickets.TicketStates.waiting_for_form


@pytest.mark.asyncio
async def test_bot_ignores_a_button_of_the_wrong_step(bot_env, monkeypatch):
    state = _state()
    await _start(bot_env, state, True, monkeypatch)
    callback = await _answer(state, bot_env, 'since', 'today')  # старая кнопка или подделка
    assert (await state.get_data()).get('diagnostics') in (None, {})
    assert await state.get_state() == bot_tickets.TicketStates.waiting_for_form
    callback.answer.assert_awaited()


@pytest.mark.asyncio
async def test_bot_ignores_an_unknown_option(bot_env, monkeypatch):
    state = _state()
    await _start(bot_env, state, True, monkeypatch)
    await _answer(state, bot_env, 'platform', 'amiga')
    assert (await state.get_data()).get('diagnostics') in (None, {})


@pytest.mark.asyncio
async def test_bot_double_tap_does_not_skip_a_question(bot_env, monkeypatch):
    state = _state()
    await _start(bot_env, state, True, monkeypatch)
    await _answer(state, bot_env, 'platform', 'ios')
    await _answer(state, bot_env, 'platform', 'android')  # второй тап по тому же экрану
    assert (await state.get_data())['diagnostics'] == {'platform': 'ios'}


@pytest.mark.asyncio
async def test_bot_form_button_outside_the_form_state_is_ignored(bot_env):
    state = _state()  # состояния нет
    callback = await _answer(state, bot_env, 'platform', 'ios')
    assert await state.get_state() is None
    callback.answer.assert_awaited()


@pytest.mark.asyncio
async def test_bot_other_asks_for_a_short_text_and_stores_it(bot_env, monkeypatch):
    state = _state()
    await _start(bot_env, state, True, monkeypatch)
    callback = await _answer(state, bot_env, 'platform', 'other')
    assert await state.get_state() == bot_tickets.TicketStates.waiting_for_form_other
    assert get_texts('ru').t('TICKET_FORM_OTHER_PROMPT').split('{')[0] in _shown_text(callback)

    message = SimpleNamespace(
        text='  Steam Deck ',
        chat=SimpleNamespace(id=10),
        message_id=90,
        answer=AsyncMock(),
        bot=SimpleNamespace(edit_message_text=AsyncMock()),
    )
    await bot_tickets.handle_ticket_form_other_input(message, state, bot_env)
    data = await state.get_data()
    assert data['diagnostics'] == {'platform': 'other', 'platform_other': 'Steam Deck'}
    assert await state.get_state() == bot_tickets.TicketStates.waiting_for_form
    shown = message.bot.edit_message_text.await_args.kwargs
    assert f'tf:app:{FORM_FIELDS["app"][0]}' in [
        b.callback_data for row in shown['reply_markup'].inline_keyboard for b in row
    ]


@pytest.mark.asyncio
async def test_bot_other_text_that_is_too_long_is_refused(bot_env, monkeypatch):
    state = _state()
    await _start(bot_env, state, True, monkeypatch)
    await _answer(state, bot_env, 'platform', 'other')
    message = SimpleNamespace(
        text='x' * (OTHER_MAX_LENGTH + 1),
        chat=SimpleNamespace(id=10),
        message_id=90,
        answer=AsyncMock(),
        bot=SimpleNamespace(edit_message_text=AsyncMock()),
    )
    await bot_tickets.handle_ticket_form_other_input(message, state, bot_env)
    assert await state.get_state() == bot_tickets.TicketStates.waiting_for_form_other
    assert 'platform' not in ((await state.get_data()).get('diagnostics') or {})


def test_only_platform_and_app_offer_other():
    assert [f for f, options in FORM_FIELDS.items() if 'other' in options] == ['platform', 'app']


@pytest.mark.asyncio
async def test_bot_creates_the_ticket_with_the_answers(bot_env, monkeypatch):
    state = _state()
    await state.set_state(bot_tickets.TicketStates.waiting_for_message)
    await state.update_data(title='Не работает VPN', diagnostics=dict(FULL), prompt_chat_id=10, prompt_message_id=77)
    monkeypatch.setattr(settings, 'SUPPORT_TICKET_FORM_REQUIRED', True)
    monkeypatch.setattr(bot_tickets.RateLimitCache, 'is_rate_limited', AsyncMock(return_value=False))
    monkeypatch.setattr(bot_tickets.cache, 'get', AsyncMock(return_value=None))
    monkeypatch.setattr(bot_tickets.cache, 'set', AsyncMock())
    monkeypatch.setattr(bot_tickets, 'notify_admins_about_new_ticket', AsyncMock())
    monkeypatch.setattr(bot_tickets, 'schedule_assist', MagicMock())
    created = SimpleNamespace(id=9, status='open', status_emoji='🟢', created_at=None)
    create = AsyncMock(return_value=created)
    monkeypatch.setattr(bot_tickets.TicketCRUD, 'create_ticket', create)
    monkeypatch.setattr(bot_tickets, 'format_local_datetime', lambda *a, **k: 'now')
    message = SimpleNamespace(
        text='Совсем не открываются сайты уже вторые сутки',
        caption=None,
        photo=None,
        video=None,
        chat=SimpleNamespace(id=10),
        message_id=91,
        answer=AsyncMock(),
        bot=SimpleNamespace(edit_message_text=AsyncMock()),
    )
    await bot_tickets.handle_ticket_message_input(message, state, bot_env, MagicMock())
    create.assert_awaited_once()
    assert create.await_args.kwargs['diagnostics'] == FULL


@pytest.mark.asyncio
async def test_bot_refuses_a_ticket_without_answers_when_the_flag_is_on(bot_env, monkeypatch):
    """Флаг включили посреди сценария: состояние осталось от старого потока без анкеты."""
    state = _state()
    await state.set_state(bot_tickets.TicketStates.waiting_for_message)
    await state.update_data(title='Не работает VPN', prompt_chat_id=10, prompt_message_id=77)
    monkeypatch.setattr(settings, 'SUPPORT_TICKET_FORM_REQUIRED', True)
    monkeypatch.setattr(bot_tickets.RateLimitCache, 'is_rate_limited', AsyncMock(return_value=False))
    monkeypatch.setattr(bot_tickets.cache, 'get', AsyncMock(return_value=None))
    monkeypatch.setattr(bot_tickets.cache, 'set', AsyncMock())
    create = AsyncMock()
    monkeypatch.setattr(bot_tickets.TicketCRUD, 'create_ticket', create)
    message = SimpleNamespace(
        text='Совсем не открываются сайты уже вторые сутки',
        caption=None,
        photo=None,
        video=None,
        chat=SimpleNamespace(id=10),
        message_id=91,
        answer=AsyncMock(),
        bot=SimpleNamespace(edit_message_text=AsyncMock()),
    )
    await bot_tickets.handle_ticket_message_input(message, state, bot_env, MagicMock())
    create.assert_not_awaited()
    assert await state.get_state() is None


@pytest.mark.asyncio
async def test_bot_flag_off_creates_without_diagnostics_kwarg_value(bot_env, monkeypatch):
    state = _state()
    await state.set_state(bot_tickets.TicketStates.waiting_for_message)
    await state.update_data(title='Не работает VPN', prompt_chat_id=10, prompt_message_id=77)
    monkeypatch.setattr(settings, 'SUPPORT_TICKET_FORM_REQUIRED', False)
    monkeypatch.setattr(bot_tickets.RateLimitCache, 'is_rate_limited', AsyncMock(return_value=False))
    monkeypatch.setattr(bot_tickets.cache, 'get', AsyncMock(return_value=None))
    monkeypatch.setattr(bot_tickets.cache, 'set', AsyncMock())
    monkeypatch.setattr(bot_tickets, 'notify_admins_about_new_ticket', AsyncMock())
    monkeypatch.setattr(bot_tickets, 'schedule_assist', MagicMock())
    monkeypatch.setattr(bot_tickets, 'format_local_datetime', lambda *a, **k: 'now')
    create = AsyncMock(return_value=SimpleNamespace(id=9, status='open', status_emoji='🟢', created_at=None))
    monkeypatch.setattr(bot_tickets.TicketCRUD, 'create_ticket', create)
    message = SimpleNamespace(
        text='Совсем не открываются сайты уже вторые сутки',
        caption=None,
        photo=None,
        video=None,
        chat=SimpleNamespace(id=10),
        message_id=91,
        answer=AsyncMock(),
        bot=SimpleNamespace(edit_message_text=AsyncMock()),
    )
    await bot_tickets.handle_ticket_message_input(message, state, bot_env, MagicMock())
    create.assert_awaited_once()
    assert create.await_args.kwargs.get('diagnostics') is None


def test_bot_handlers_are_registered():
    from aiogram import Dispatcher

    dp = Dispatcher()
    bot_tickets.register_handlers(dp)
    callbacks = [h.callback.__name__ for h in dp.callback_query.handlers]
    messages = [h.callback.__name__ for h in dp.message.handlers]
    assert 'handle_ticket_form_answer' in callbacks
    assert 'handle_ticket_form_other_input' in messages


# --- уведомление владельцу ---------------------------------------------------


@pytest.mark.asyncio
async def test_new_ticket_notification_shows_the_answers(monkeypatch):
    sent = {}

    class FakeService:
        def __init__(self, bot):
            pass

        async def send_ticket_event_notification(self, text, keyboard, **kwargs):
            sent['text'] = text
            return True

    monkeypatch.setattr(bot_tickets, 'AdminNotificationService', FakeService)
    monkeypatch.setattr(bot_tickets, '_build_ticket_notification_keyboard', lambda *a, **k: None)
    monkeypatch.setattr(bot_tickets, 'get_user_by_id', AsyncMock(return_value=None))
    monkeypatch.setattr(
        bot_tickets.TicketMessageCRUD,
        'get_first_message',
        AsyncMock(return_value=SimpleNamespace(has_media=False, message_text='не работает', media_file_id=None)),
    )
    monkeypatch.setattr(type(settings), 'is_admin_notifications_enabled', lambda self: True)
    from app.services import maintenance_service as ms

    monkeypatch.setattr(ms.maintenance_service, '_bot', object(), raising=False)

    ticket = SimpleNamespace(
        id=3,
        user_id=1,
        title='Не работает',
        created_at=None,
        diagnostics={**FULL, 'platform': 'other', 'platform_other': 'Steam <b>Deck</b>'},
    )
    monkeypatch.setattr(bot_tickets, 'format_local_datetime', lambda *a, **k: 'now')
    await bot_tickets.notify_admins_about_new_ticket(ticket, MagicMock())

    text = sent['text']
    assert 'Happ' in text
    assert 'Steam &lt;b&gt;Deck&lt;/b&gt;' in text  # свободный текст клиента экранирован
    assert '<b>Deck</b>' not in text


@pytest.mark.asyncio
async def test_new_ticket_notification_without_answers_is_unchanged(monkeypatch):
    sent = {}

    class FakeService:
        def __init__(self, bot):
            pass

        async def send_ticket_event_notification(self, text, keyboard, **kwargs):
            sent['text'] = text
            return True

    monkeypatch.setattr(bot_tickets, 'AdminNotificationService', FakeService)
    monkeypatch.setattr(bot_tickets, '_build_ticket_notification_keyboard', lambda *a, **k: None)
    monkeypatch.setattr(bot_tickets, 'get_user_by_id', AsyncMock(return_value=None))
    monkeypatch.setattr(
        bot_tickets.TicketMessageCRUD,
        'get_first_message',
        AsyncMock(return_value=SimpleNamespace(has_media=False, message_text='не работает', media_file_id=None)),
    )
    monkeypatch.setattr(type(settings), 'is_admin_notifications_enabled', lambda self: True)
    from app.services import maintenance_service as ms

    monkeypatch.setattr(ms.maintenance_service, '_bot', object(), raising=False)
    monkeypatch.setattr(bot_tickets, 'format_local_datetime', lambda *a, **k: 'now')

    ticket = SimpleNamespace(id=3, user_id=1, title='Не работает', created_at=None, diagnostics=None)
    await bot_tickets.notify_admins_about_new_ticket(ticket, MagicMock())
    assert 'Анкета' not in sent['text']
