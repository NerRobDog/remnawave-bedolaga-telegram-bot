import asyncio
import contextlib
import inspect
import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.dialects import postgresql

from app.config import settings
from app.database.models import Ticket, TicketMessage
from app.services.support_assist import pipeline
from app.services.support_assist.snapshot import Snapshot
from tests.services.support_assist.fixtures import AS_OF, healthy, s05


NOW = AS_OF
VALID = {
    'facts': [{'icon': '✅', 'text': 'Подписка активна'}],
    'cause': 'Нужны детали',
    'owner_action': 'нет',
    'ask_client': ['Какое приложение?'],
    'draft': 'Уточните, пожалуйста, устройство.',
}


def _response(text):
    usage = SimpleNamespace(
        input_tokens=1000, output_tokens=500, cache_read_input_tokens=2000, cache_creation_input_tokens=0
    )
    return SimpleNamespace(
        content=[SimpleNamespace(type='text', text=text)],
        usage=usage,
        stop_reason='end_turn',
        model='claude-opus-5-5',
    )


def _client(exc=None):
    client = MagicMock()
    client.beta.messages.create = AsyncMock(return_value=None if exc else _response(json.dumps(VALID)), side_effect=exc)
    return client


def _msg(i, text, *, admin=False, media=False):
    return SimpleNamespace(
        id=i,
        is_from_admin=admin,
        is_system=False,
        message_text=text,
        has_media=media,
        media_type='photo' if media else None,
        created_at=NOW,
    )


def _ticket(text='не работает', *, media=False, lang='ru'):
    return SimpleNamespace(
        id=1,
        user_id=7,
        status='open',
        updated_at=NOW - timedelta(hours=2),
        user=SimpleNamespace(language=lang),
        messages=[_msg(1, text, media=media)],
    )


class Env:
    """Подменённые границы конвейера: БД-функции, снимок, бот и уведомление клиенту."""

    def __init__(self):
        self.cards: list[SimpleNamespace] = []
        self.claimed: set[int] = set()
        self.snapshot: Snapshot | Exception = healthy()
        self.ticket = _ticket()
        self.notify = AsyncMock(side_effect=self._read_something)
        self.bot = MagicMock()
        self.bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=555))
        self.db = MagicMock()
        # in_tx имитирует занятое соединение: чтение или commit+refresh открывают транзакцию, commit закрывает.
        self.db.in_tx = False
        self.db.commit = AsyncMock(side_effect=self._commit)
        self.db.rollback = AsyncMock(side_effect=self._commit)
        self.db.add = MagicMock(side_effect=lambda obj: setattr(obj, 'id', 321))

    async def _commit(self):
        self.db.in_tx = False

    async def _read_something(self, *args, **kwargs):
        self.db.in_tx = True  # notify_user_about_ticket_reply читает последнее сообщение тикета


@pytest.fixture(autouse=True)
def cabinet(monkeypatch):
    """Три кабинетных побочных эффекта ручного ответа: событие, запись уведомления, push по WebSocket."""
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
def env(monkeypatch):
    e = Env()
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

    async def last_card(db, ticket_id):
        db.in_tx = True
        rows = [c for c in e.cards if c.ticket_id == ticket_id]
        return rows[-1] if rows else None

    async def create_card(db, **fields):
        await asyncio.sleep(0)
        row = SimpleNamespace(id=len(e.cards) + 1, **fields)
        e.cards.append(row)
        db.in_tx = True  # commit() + refresh() оставляют транзакцию открытой
        return row

    async def mark_auto_ask(db, card_id, question_ids):
        row = next(c for c in e.cards if c.id == card_id)
        if row.ticket_id in e.claimed:
            return False  # частичный уникальный индекс: у тикета уже есть автовопрос
        e.claimed.add(row.ticket_id)
        row.auto_ask_question_ids = question_ids
        return True

    async def collect(db, api, ticket, now):
        if isinstance(e.snapshot, Exception):
            raise e.snapshot
        return e.snapshot

    monkeypatch.setattr(pipeline, 'last_card_for_ticket', last_card)
    monkeypatch.setattr(pipeline, 'create_card', create_card)
    monkeypatch.setattr(pipeline, 'mark_auto_ask_sent', mark_auto_ask)
    monkeypatch.setattr(pipeline, 'collect_snapshot', collect)
    monkeypatch.setattr(pipeline.TicketCRUD, 'get_ticket_by_id', AsyncMock(side_effect=lambda *a, **k: e.ticket))
    monkeypatch.setattr('app.handlers.admin.tickets.notify_user_about_ticket_reply', e.notify)
    return e


async def _run(e, *, client='ok', trigger='new_ticket', now=NOW):
    client = _client() if client == 'ok' else client
    return await pipeline.run_assist(e.db, e.bot, MagicMock(), client, 1, trigger, now)


def _callbacks(e):
    markup = e.bot.send_message.call_args.kwargs['reply_markup']
    return [b.callback_data for row in markup.inline_keyboard for b in row]


# --- schedule_assist ------------------------------------------------------


def test_flag_off_creates_no_task(monkeypatch):
    monkeypatch.setattr(settings, 'SUPPORT_ASSIST_ENABLED', False)
    create_task = MagicMock()
    monkeypatch.setattr(pipeline.asyncio, 'create_task', create_task)
    pipeline.schedule_assist(MagicMock(), 1, 'new_ticket')
    create_task.assert_not_called()


@pytest.mark.asyncio
async def test_flag_on_schedules_task_and_holds_strong_ref(monkeypatch):
    monkeypatch.setattr(settings, 'SUPPORT_ASSIST_ENABLED', True)
    calls = []

    async def fake_run_safe(bot, ticket_id, trigger):
        calls.append((ticket_id, trigger))

    monkeypatch.setattr(pipeline, '_run_safe', fake_run_safe)
    pipeline.schedule_assist(MagicMock(), 5, 'new_message')
    assert len(pipeline._background_tasks) == 1
    await asyncio.gather(*list(pipeline._background_tasks))
    await asyncio.sleep(0)
    assert calls == [(5, 'new_message')]
    assert not pipeline._background_tasks


@pytest.mark.asyncio
async def test_schedule_without_bot_is_a_noop(monkeypatch):
    monkeypatch.setattr(settings, 'SUPPORT_ASSIST_ENABLED', True)
    pipeline.schedule_assist(None, 1, 'new_ticket')
    assert not pipeline._background_tasks


@pytest.mark.asyncio
async def test_run_safe_swallows_everything(monkeypatch):
    def boom():
        raise RuntimeError('db down')

    monkeypatch.setattr(pipeline, 'AsyncSessionLocal', boom)
    await pipeline._run_safe(MagicMock(), 1, 'new_ticket')  # не бросает


# --- run_assist: карточка --------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_sends_one_card_to_the_topic_and_stores_it(env):
    card = await _run(env)
    env.bot.send_message.assert_awaited_once()
    kwargs = env.bot.send_message.call_args.kwargs
    assert kwargs['chat_id'] == -100500
    assert kwargs['message_thread_id'] == 77
    assert kwargs['parse_mode'] == 'HTML'
    assert kwargs['text'].startswith('<b>Тикет #1</b>')
    assert card is env.cards[0]
    assert card.status == 'shown'
    assert card.admin_message_id == 555
    assert card.card['cause'] == VALID['cause']
    assert (card.model, card.input_tokens, card.output_tokens, card.cache_read_tokens) == (
        'claude-opus-5-5',
        1000,
        500,
        2000,
    )
    assert float(card.cost_usd) > 0
    assert card.trigger == 'new_ticket'
    assert {f['sev'] for f in card.flags} <= {'low', 'info'}  # здоровый клиент: ничего серьёзного
    assert all({'code', 'sev', 'text'} <= set(f) for f in card.flags)
    env.notify.assert_not_awaited()  # автовопрос выключен


@pytest.mark.asyncio
async def test_client_exception_gives_fallback_card_and_still_sends(env):
    card = await _run(env, client=_client(exc=RuntimeError('boom')))
    env.bot.send_message.assert_awaited_once()
    assert 'разбор без модели' in env.bot.send_message.call_args.kwargs['text']
    assert card.status == 'fallback'
    assert card.card is None
    assert card.admin_message_id == 555


@pytest.mark.asyncio
async def test_no_api_key_gives_fallback_card(env):
    card = await _run(env, client=None)
    assert card.status == 'fallback'
    env.bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_snapshot_source_failure_still_sends_card(env):
    env.snapshot = healthy(missing=['devices'], devices=[])
    card = await _run(env)
    env.bot.send_message.assert_awaited_once()
    assert card.status == 'shown'


@pytest.mark.asyncio
async def test_snapshot_collector_crash_still_sends_card(env):
    env.snapshot = RuntimeError('collector bug')
    card = await _run(env)
    env.bot.send_message.assert_awaited_once()
    assert card.status == 'shown'


def _sent_text(e):
    return e.bot.send_message.call_args.kwargs['text']


def _flag_codes(card):
    return {f['code'] for f in card.flags}


PANEL_SOURCES = ['panel_user', 'sub_requests', 'devices', 'usage', 'squad_nodes', 'nodes']


@pytest.mark.asyncio
async def test_collector_crash_says_no_data_not_no_subscription(env):
    env.snapshot = RuntimeError('collector bug')
    card = await _run(env, client=None)
    text = _sent_text(env)
    assert 'no_subscription' not in _flag_codes(card)
    assert 'data_unavailable' in _flag_codes(card)
    assert 'нет подписки' not in text
    assert '❓ Нет данных' in text


@pytest.mark.asyncio
async def test_collector_crash_payload_to_model_carries_no_false_subscription_fact(env):
    env.snapshot = RuntimeError('collector bug')
    client = _client()
    card = await _run(env, client=client)
    sent = json.dumps(client.beta.messages.create.call_args.kwargs['messages'], ensure_ascii=False)
    assert 'no_subscription' not in sent
    assert 'У клиента нет подписки' not in sent
    assert 'data_unavailable' in sent
    assert card.status == 'shown'


@pytest.mark.asyncio
async def test_bot_db_failure_says_no_data_not_no_subscription(env):
    # источник bot_db упал (например, пул соединений исчерпан): subscription=None, но это не «нет подписки»
    env.snapshot = Snapshot(as_of=NOW, missing=['bot_db'])
    card = await _run(env, client=None)
    assert 'no_subscription' not in _flag_codes(card)
    assert 'нет подписки' not in _sent_text(env)
    assert '❓ Нет данных' in _sent_text(env)


@pytest.mark.asyncio
async def test_panel_down_fallback_card_has_unavailable_line(env):
    env.snapshot = healthy(
        panel_user=None,
        devices=[],
        sub_requests=[],
        sub_requests_total=None,
        usage_by_day={},
        squad_nodes=[],
        missing=PANEL_SOURCES,
    )
    card = await _run(env, client=None)
    text = _sent_text(env)
    assert _flag_codes(card) == {'data_unavailable'}
    assert '❓ Нет данных' in text
    assert 'устройства' in text and 'профиль в панели' in text
    assert 'нет подписки' not in text  # подписка в боте есть, панель недоступна


@pytest.mark.asyncio
async def test_real_no_subscription_is_reported(env):
    env.snapshot = Snapshot(as_of=NOW, account={'id': 9001, 'status': 'active', 'language': 'ru'}, subscription=None)
    card = await _run(env, client=None)
    assert _flag_codes(card) == {'no_subscription'}
    assert '🔴 У клиента нет подписки в боте' in _sent_text(env)
    assert '❓' not in _sent_text(env)


@pytest.mark.asyncio
async def test_second_run_within_throttle_window_does_nothing(env):
    assert await _run(env) is not None
    again = await _run(env, now=NOW + timedelta(minutes=9))
    assert again is None
    assert env.bot.send_message.await_count == 1
    assert len(env.cards) == 1
    # после окна пересчёт идёт
    later = await _run(env, now=NOW + timedelta(minutes=11), trigger='new_message')
    assert later is not None
    assert env.bot.send_message.await_count == 2


@pytest.mark.asyncio
async def test_send_failure_keeps_tokens_and_marks_failed(env):
    env.bot.send_message = AsyncMock(side_effect=RuntimeError('topic gone'))
    card = await _run(env)
    assert card.status == 'failed'
    assert card.admin_message_id is None
    assert card.cost_usd is not None and card.input_tokens == 1000


@pytest.mark.asyncio
async def test_admin_notifications_off_does_no_work(env, monkeypatch):
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_ENABLED', False)
    client = _client()
    assert await _run(env, client=client) is None
    client.beta.messages.create.assert_not_awaited()
    env.bot.send_message.assert_not_awaited()
    assert env.cards == []


@pytest.mark.asyncio
async def test_keyboard_follows_send_buttons_flag(env, monkeypatch):
    await _run(env)
    assert _callbacks(env) == ['sa:skip:1']

    monkeypatch.setattr(settings, 'SUPPORT_ASSIST_SEND_BUTTONS_ENABLED', True)
    await _run(env, now=NOW + timedelta(minutes=30), trigger='new_message')
    assert _callbacks(env) == ['sa:send:2', 'sa:edit:2', 'sa:skip:2']

    # без черновика (карточка без модели) отправлять нечего
    await _run(env, client=None, now=NOW + timedelta(minutes=60), trigger='new_message')
    assert _callbacks(env) == ['sa:skip:3']


@pytest.mark.asyncio
async def test_payload_sent_to_model_has_no_client_identity(env):
    client = _client()
    await _run(env, client=client)
    sent = json.dumps(client.beta.messages.create.call_args.kwargs['messages'], ensure_ascii=False)
    assert 'не работает' in sent  # текст клиента уходит как есть
    assert '9001' not in sent  # id аккаунта из снимка не уходит


# --- run_assist: автовопрос ------------------------------------------------


@pytest.mark.asyncio
async def test_auto_ask_off_sends_nothing_to_client(env, cabinet):
    await _run(env)
    env.notify.assert_not_awaited()
    env.db.add.assert_not_called()
    cabinet.emit.assert_not_awaited()
    cabinet.notification.assert_not_awaited()
    cabinet.push.assert_not_awaited()


@pytest.mark.asyncio
async def test_auto_ask_sends_questions_as_system_message_and_keeps_ticket_open(env, monkeypatch):
    monkeypatch.setattr(settings, 'SUPPORT_ASSIST_AUTO_ASK_ENABLED', True)
    updated_before = env.ticket.updated_at
    card = await _run(env)

    env.notify.assert_awaited_once()
    bot, ticket, text, db = env.notify.call_args.args
    assert (bot, ticket, db) == (env.bot, env.ticket, env.db)
    env.db.add.assert_called_once()
    stored = env.db.add.call_args.args[0]
    assert isinstance(stored, TicketMessage)
    assert stored.is_from_admin is True and stored.is_system is True
    assert stored.ticket_id == 1 and stored.message_text == text
    assert text.count('\n') >= 2  # вступление и нумерованные вопросы
    assert env.ticket.status == 'open'
    assert env.ticket.updated_at == updated_before
    assert card.auto_ask_question_ids and 'device' not in card.auto_ask_question_ids  # устройства известны
    assert card.status == 'shown'


@pytest.mark.asyncio
async def test_auto_ask_only_on_new_ticket_trigger(env, monkeypatch):
    monkeypatch.setattr(settings, 'SUPPORT_ASSIST_AUTO_ASK_ENABLED', True)
    await _run(env, trigger='new_message')
    env.notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_auto_ask_skips_a_detailed_first_message(env, monkeypatch):
    monkeypatch.setattr(settings, 'SUPPORT_ASSIST_AUTO_ASK_ENABLED', True)
    env.ticket = _ticket(
        'Вчера вечером перестал открываться Instagram на iPhone через мобильный интернет МТС, '
        'в Wi-Fi дома всё работает, приложение Happ показывает подключено, перезапуск не помог, '
        'на ноутбуке с macOS те же сайты открываются без проблем'
    )
    await _run(env)
    env.notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_our_fault_flag_means_no_client_message(env, monkeypatch):
    monkeypatch.setattr(settings, 'SUPPORT_ASSIST_AUTO_ASK_ENABLED', True)
    env.snapshot = s05()  # подписка истекла: вопросы клиенту не нужны
    card = await _run(env)
    env.notify.assert_not_awaited()
    assert card.status == 'shown'  # карточка владельцу всё равно ушла


@pytest.mark.asyncio
async def test_concurrent_runs_ask_the_client_once(env, monkeypatch):
    monkeypatch.setattr(settings, 'SUPPORT_ASSIST_AUTO_ASK_ENABLED', True)
    results = await asyncio.gather(_run(env), _run(env))
    assert env.notify.await_count == 1
    assert env.bot.send_message.await_count == 1
    assert [r is None for r in results].count(True) == 1


@pytest.mark.asyncio
async def test_auto_ask_claim_lost_in_db_sends_nothing(env, cabinet, monkeypatch):
    """Страховка на случай второго процесса: уникальный индекс не даёт спросить дважды."""
    monkeypatch.setattr(settings, 'SUPPORT_ASSIST_AUTO_ASK_ENABLED', True)
    monkeypatch.setattr(settings, 'SUPPORT_ASSIST_RECOMPUTE_MINUTES', 0)
    await _run(env)
    await _run(env, now=NOW + timedelta(seconds=1))
    assert env.notify.await_count == 1
    assert cabinet.emit.await_count == 1 and cabinet.push.await_count == 1
    assert env.bot.send_message.await_count == 2  # карточка новая, вопросы — нет


@pytest.mark.asyncio
async def test_auto_ask_failure_does_not_block_the_card(env, monkeypatch):
    monkeypatch.setattr(settings, 'SUPPORT_ASSIST_AUTO_ASK_ENABLED', True)
    env.notify.side_effect = RuntimeError('telegram down')
    card = await _run(env)
    env.bot.send_message.assert_awaited_once()
    assert card.status == 'shown'


@pytest.mark.asyncio
async def test_photo_only_first_message_is_bare_but_gets_no_screenshot_question(env, monkeypatch):
    monkeypatch.setattr(settings, 'SUPPORT_ASSIST_AUTO_ASK_ENABLED', True)
    env.ticket = _ticket('', media=True)
    card = await _run(env)
    assert card.auto_ask_question_ids
    assert 'screenshot' not in card.auto_ask_question_ids


@pytest.mark.asyncio
async def test_questions_come_in_the_users_language(env, monkeypatch):
    monkeypatch.setattr(settings, 'SUPPORT_ASSIST_AUTO_ASK_ENABLED', True)
    env.ticket = _ticket('not working', lang='en')
    env.snapshot = healthy(account={'id': 9001, 'status': 'active', 'language': 'en'})
    await _run(env)
    text = env.notify.call_args.args[2]
    assert not any('а' <= ch.lower() <= 'я' for ch in text)  # без кириллицы


# --- автовопрос: доставка в кабинет ------------------------------------------


@pytest.mark.asyncio
async def test_auto_ask_is_also_delivered_in_the_cabinet(env, cabinet, monkeypatch):
    """Как ручной ответ из кабинета (admin_tickets.reply_to_ticket): событие, уведомление, push."""
    monkeypatch.setattr(settings, 'SUPPORT_ASSIST_AUTO_ASK_ENABLED', True)
    updated_before = env.ticket.updated_at
    await _run(env)

    body = env.notify.call_args.args[2]
    cabinet.emit.assert_awaited_once()
    event, payload = cabinet.emit.call_args.args
    assert event == 'ticket.message_added'
    assert (
        payload
        == {
            'ticket_id': 1,
            'message_id': 321,  # id сохранённого системного сообщения
            'user_id': 7,
            'is_from_admin': True,
            'message_text': body[:200],
            'has_media': False,
            'status': 'open',
        }
    )
    assert cabinet.emit.call_args.kwargs == {'db': env.db}
    cabinet.notification.assert_awaited_once_with(env.db, env.ticket, body)
    cabinet.push.assert_awaited_once_with(7, 1, body[:100])
    assert env.ticket.status == 'open' and env.ticket.updated_at == updated_before


@pytest.mark.asyncio
async def test_cabinet_delivery_survives_a_telegram_failure(env, cabinet, monkeypatch):
    monkeypatch.setattr(settings, 'SUPPORT_ASSIST_AUTO_ASK_ENABLED', True)
    env.notify.side_effect = RuntimeError('telegram down')
    card = await _run(env)
    cabinet.emit.assert_awaited_once()
    cabinet.push.assert_awaited_once()
    assert card.status == 'shown'


@pytest.mark.asyncio
async def test_event_failure_does_not_block_telegram_or_cabinet_notification(env, cabinet, monkeypatch):
    monkeypatch.setattr(settings, 'SUPPORT_ASSIST_AUTO_ASK_ENABLED', True)
    cabinet.emit.side_effect = RuntimeError('listener crashed')
    card = await _run(env)
    env.notify.assert_awaited_once()
    cabinet.notification.assert_awaited_once()
    cabinet.push.assert_awaited_once()
    assert card.status == 'shown'


@pytest.mark.asyncio
async def test_no_push_when_cabinet_notifications_are_off(env, cabinet, monkeypatch):
    monkeypatch.setattr(settings, 'SUPPORT_ASSIST_AUTO_ASK_ENABLED', True)
    cabinet.notification.return_value = None  # SupportSettingsService: кабинетные уведомления выключены
    await _run(env)
    cabinet.emit.assert_awaited_once()
    cabinet.push.assert_not_awaited()


@pytest.mark.asyncio
async def test_notification_failure_does_not_block_the_card(env, cabinet, monkeypatch):
    monkeypatch.setattr(settings, 'SUPPORT_ASSIST_AUTO_ASK_ENABLED', True)
    cabinet.notification.side_effect = RuntimeError('db error')
    card = await _run(env)
    cabinet.push.assert_not_awaited()
    assert card.status == 'shown'


# --- соединение с БД не держится на время сети --------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize('auto_ask', [False, True])
async def test_no_transaction_is_open_during_model_call_and_card_send(env, monkeypatch, auto_ask):
    """Транзакция держит соединение пула; idle_in_transaction_session_timeout на проде — 300 с."""
    monkeypatch.setattr(settings, 'SUPPORT_ASSIST_AUTO_ASK_ENABLED', auto_ask)
    seen = {}
    real_generate = pipeline.generate_card

    async def spy_generate(client, payload, texts):
        seen['generate'] = env.db.in_tx
        return await real_generate(client, payload, texts)

    async def spy_send(**kwargs):
        seen['send'] = env.db.in_tx
        return SimpleNamespace(message_id=555)

    monkeypatch.setattr(pipeline, 'generate_card', spy_generate)
    env.bot.send_message = AsyncMock(side_effect=spy_send)

    card = await _run(env)
    assert seen == {'generate': False, 'send': False}
    assert card.status == 'shown'
    assert env.notify.await_count == (1 if auto_ask else 0)


@pytest.mark.asyncio
async def test_snapshot_hang_is_cut_off_and_card_still_sent(env, monkeypatch):
    monkeypatch.setattr(pipeline, 'SNAPSHOT_TIMEOUT_SECONDS', 0.05)

    async def hang(db, api, ticket, now):
        db.in_tx = True
        await asyncio.sleep(3600)

    monkeypatch.setattr(pipeline, 'collect_snapshot', hang)
    card = await asyncio.wait_for(_run(env, client=None), 5)
    assert card.status == 'fallback'
    assert 'snapshot' in card.facts['missing']
    assert 'no_subscription' not in _flag_codes(card)  # обрыв по таймауту не означает «подписки нет»
    assert '❓ Нет данных' in _sent_text(env)
    env.bot.send_message.assert_awaited_once()
    env.db.commit.assert_awaited()  # транзакция, оставшаяся от оборванного сбора, закрыта до записи карточки


@pytest.mark.asyncio
async def test_failed_session_after_snapshot_timeout_is_rolled_back(env, monkeypatch):
    monkeypatch.setattr(pipeline, 'SNAPSHOT_TIMEOUT_SECONDS', 0.05)

    async def hang(db, api, ticket, now):
        await asyncio.sleep(3600)

    monkeypatch.setattr(pipeline, 'collect_snapshot', hang)
    env.db.commit.side_effect = [RuntimeError('PendingRollbackError'), None, None, None]
    card = await asyncio.wait_for(_run(env), 5)
    assert card.status == 'shown'
    env.db.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_safe_caps_concurrent_pipelines(monkeypatch):
    # свежий семафор: у модульного после состязания в одном цикле событий остаётся привязка к нему
    monkeypatch.setattr(pipeline, '_pipeline_slots', asyncio.Semaphore(2))
    monkeypatch.setattr(settings, 'ANTHROPIC_API_KEY', None)
    state = {'running': 0, 'peak': 0, 'started': 0}

    @contextlib.asynccontextmanager
    async def fake_ctx(*args, **kwargs):
        yield MagicMock()

    async def fake_run(db, bot, api, client, ticket_id, trigger, now):
        state['running'] += 1
        state['started'] += 1
        state['peak'] = max(state['peak'], state['running'])
        await asyncio.sleep(0.01)
        state['running'] -= 1

    monkeypatch.setattr(pipeline, 'AsyncSessionLocal', fake_ctx)
    monkeypatch.setattr('app.services.remnawave_service.remnawave_service.get_api_client', fake_ctx)
    monkeypatch.setattr(pipeline, 'run_assist', fake_run)
    await asyncio.gather(*[pipeline._run_safe(MagicMock(), i, 'new_ticket') for i in range(6)])
    assert state['started'] == 6
    assert state['peak'] == 2


# --- SLA --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sla_still_reminds_after_auto_ask(monkeypatch):
    """Автовопрос не должен гасить SLA-напоминание: тикет остаётся OPEN с прежним updated_at,
    а выборка SLA вообще не смотрит на сообщения тикета."""
    from app.services.monitoring_service import MonitoringService
    from app.services.support_settings_service import SupportSettingsService

    e = Env()
    # реальные ORM-объекты: проверяем, что пайплайн не трогает строку тикета
    ticket = Ticket(id=1, user_id=7, title='t', status='open', updated_at=NOW - timedelta(hours=2))
    ticket.messages = [
        TicketMessage(id=1, ticket_id=1, user_id=7, message_text='не работает', is_from_admin=False, has_media=False)
    ]
    ticket.messages[0].is_system = False
    e.ticket = ticket
    updated_before = ticket.updated_at
    for name, value in {
        'SUPPORT_ASSIST_AUTO_ASK_ENABLED': True,
        'ADMIN_NOTIFICATIONS_ENABLED': True,
        'ADMIN_NOTIFICATIONS_CHAT_ID': '-100500',
        'SUPPORT_ASSIST_RECOMPUTE_MINUTES': 10,
    }.items():
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(pipeline, 'last_card_for_ticket', AsyncMock(return_value=None))
    monkeypatch.setattr(pipeline, 'create_card', AsyncMock(side_effect=lambda db, **f: SimpleNamespace(id=1, **f)))
    monkeypatch.setattr(pipeline, 'mark_auto_ask_sent', AsyncMock(return_value=True))
    monkeypatch.setattr(pipeline, 'collect_snapshot', AsyncMock(return_value=healthy()))
    monkeypatch.setattr(pipeline.TicketCRUD, 'get_ticket_by_id', AsyncMock(return_value=ticket))
    monkeypatch.setattr('app.handlers.admin.tickets.notify_user_about_ticket_reply', e.notify)

    await pipeline.run_assist(e.db, e.bot, MagicMock(), _client(), 1, 'new_ticket', NOW)
    e.notify.assert_awaited_once()
    assert e.db.add.call_args.args[0].is_system is True
    assert ticket.status == 'open'
    assert ticket.updated_at == updated_before

    # выборка SLA: статус OPEN + давность updated_at, без обращения к ticket_messages
    monkeypatch.setattr(SupportSettingsService, 'get_sla_enabled', classmethod(lambda cls: True))
    statements = []
    sla_db = MagicMock()

    async def capture(stmt, *a, **k):
        statements.append(stmt)
        result = MagicMock()
        result.scalars.return_value.all.return_value = []
        return result

    sla_db.execute = capture
    await MonitoringService(bot=MagicMock())._check_ticket_sla(sla_db)
    assert len(statements) == 1
    sql = str(statements[0].compile(dialect=postgresql.dialect()))
    assert 'ticket_messages' not in sql
    assert 'tickets.status' in sql and 'tickets.updated_at' in sql


# --- хуки -------------------------------------------------------------------


@pytest.mark.parametrize(
    ('module', 'function', 'trigger'),
    [
        ('app.handlers.tickets', 'handle_ticket_message_input', 'new_ticket'),
        ('app.handlers.tickets', 'handle_ticket_reply', 'new_message'),
        ('app.cabinet.routes.tickets', 'create_ticket', 'new_ticket'),
        ('app.cabinet.routes.tickets', 'add_ticket_message', 'new_message'),
        ('app.cabinet.routes.support_ws', '_handle_ticket_reply', 'new_message'),
    ],
)
def test_hook_is_wired(module, function, trigger):
    import importlib

    source = inspect.getsource(getattr(importlib.import_module(module), function))
    assert 'schedule_assist(' in source
    assert f"'{trigger}'" in source
