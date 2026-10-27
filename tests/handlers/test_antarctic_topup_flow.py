"""Antarctic Wallet в боте: кнопка, запрос суммы, счёт со ссылкой на мини-приложение."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from app.config import settings
from app.handlers.balance import antarctic as antarctic_handlers
from app.keyboards.inline import get_payment_methods_keyboard
from app.keyboards.topup_amounts import resolve_config_method_id
from app.states import BalanceStates


APP_LINK = 'https://aw.me/app/1?start_param=tok'
REFERRAL = 'https://antarcticwallet.com/ref/abc'


@pytest.fixture(autouse=True)
def _enable(monkeypatch):
    for key, value in {
        'ANTARCTIC_ENABLED': True,
        'ANTARCTIC_APP_ID': 'app1',
        'ANTARCTIC_APP_SECRET': 'secret',
    }.items():
        monkeypatch.setattr(settings, key, value, raising=False)


def _callbacks(markup) -> list[str]:
    return [button.callback_data for row in markup.inline_keyboard for button in row if button.callback_data]


def _state() -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=7, user_id=7))


def _user(restricted: bool = False):
    return SimpleNamespace(id=7, telegram_id=7, language='ru', restriction_topup=restricted, restriction_reason=None)


def _callback(data: str):
    message = MagicMock()
    message.edit_text = AsyncMock()
    message.message_id = 10
    message.chat = SimpleNamespace(id=7)
    return SimpleNamespace(data=data, message=message, answer=AsyncMock(), bot=MagicMock())


def _amount_message():
    message = MagicMock()
    message.answer = AsyncMock()
    message.delete = AsyncMock()
    message.chat = SimpleNamespace(id=7)
    message.bot = MagicMock()
    return message


def _service(result):
    class _Service:
        calls: list = []

        def __init__(self, _bot=None):
            pass

        async def create_antarctic_payment(self, *_args, **kwargs):
            _Service.calls.append(kwargs)
            return result

    return _Service


def _result(referral=REFERRAL):
    return {
        'local_payment_id': 7,
        'token': 'tok',
        'amount_kopeks': 10000,
        'amount_usd': '1.05',
        'app_link': APP_LINK,
        'pay_page_url': 'https://bot.example/antarctic/pay?t=tok',
        'referral_url': referral,
        'expires_at': '2026-10-24T12:00:00+00:00',
    }


def test_single_button_in_methods_keyboard():
    assert 'topup_antarctic' in _callbacks(get_payment_methods_keyboard(0, 'ru'))


def test_prefilled_amount_keeps_method_in_callback():
    assert 'topup_amount|antarctic|50000' in _callbacks(get_payment_methods_keyboard(50000, 'ru'))


def test_quick_amounts_resolve_to_antarctic_config():
    assert resolve_config_method_id('antarctic') == 'antarctic'


def test_hidden_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, 'ANTARCTIC_ENABLED', False, raising=False)
    assert not [c for c in _callbacks(get_payment_methods_keyboard(0, 'ru')) if 'antarctic' in c]


@pytest.mark.asyncio
async def test_start_asks_for_amount(monkeypatch):
    monkeypatch.setattr(antarctic_handlers, 'get_topup_amount_keyboard', AsyncMock(return_value=None))
    state = _state()
    callback = _callback('topup_antarctic')

    await antarctic_handlers.start_antarctic_payment(callback, _user(), state)

    assert (await state.get_data())['payment_method'] == 'antarctic'
    assert await state.get_state() == BalanceStates.waiting_for_amount.state
    assert 'Antarctic Wallet' in callback.message.edit_text.call_args.args[0]


@pytest.mark.asyncio
async def test_start_refused_for_restricted_user():
    state = _state()
    callback = _callback('topup_antarctic')

    await antarctic_handlers.start_antarctic_payment(callback, _user(restricted=True), state)

    assert 'Пополнение ограничено' in callback.message.edit_text.call_args.args[0]
    assert await state.get_state() is None


@pytest.mark.asyncio
async def test_start_refused_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, 'ANTARCTIC_ENABLED', False, raising=False)
    callback = _callback('topup_antarctic')

    await antarctic_handlers.start_antarctic_payment(callback, _user(), _state())

    assert callback.answer.call_args.kwargs.get('show_alert') is True
    callback.message.edit_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_invoice_has_pay_button_then_referral_then_check(monkeypatch):
    monkeypatch.setattr(antarctic_handlers, 'PaymentService', _service(_result()))
    message = _amount_message()

    await antarctic_handlers.process_antarctic_payment_amount(message, _user(), object(), 10000, _state())

    sent = message.answer.call_args
    assert '$1.05' in sent.args[0]
    assert 'Нет кошелька? Создайте за минуту' in sent.args[0]
    rows = sent.kwargs['reply_markup'].inline_keyboard
    assert rows[0][0].url == APP_LINK
    assert rows[0][0].text == '💳 Оплатить в Antarctic Wallet'
    assert rows[1][0].url == REFERRAL
    assert rows[2][0].callback_data == 'check_antarctic_7'
    assert rows[3][0].callback_data == 'balance_topup'


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('minutes', 'phrase'),
    [(30, 'Ссылка действует 30 минут'), (1, 'Ссылка действует 1 минуту'), (45, 'Ссылка действует 45 минут')],
)
async def test_invoice_shows_the_configured_link_lifetime(monkeypatch, minutes, phrase):
    monkeypatch.setattr(settings, 'ANTARCTIC_PAYMENT_TTL_MINUTES', minutes, raising=False)
    monkeypatch.setattr(antarctic_handlers, 'PaymentService', _service(_result()))
    message = _amount_message()

    await antarctic_handlers.process_antarctic_payment_amount(message, _user(), object(), 10000, _state())

    text = message.answer.call_args.args[0]
    assert phrase in text
    assert '{' not in text


_LOCALES = Path(antarctic_handlers.__file__).resolve().parents[2] / 'localization' / 'locales'


@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa'])
def test_every_locale_takes_the_link_lifetime_from_a_placeholder(language):
    """Срок жизни ссылки задаёт ANTARCTIC_PAYMENT_TTL_MINUTES, а не цифра в тексте перевода."""
    template = json.loads((_LOCALES / f'{language}.json').read_text(encoding='utf-8'))['ANTARCTIC_PAYMENT_CREATED']

    assert '{ttl}' in template
    assert '30' not in template
    assert '۳۰' not in template
    rendered = template.format(name='AW', amount='100 ₽', amount_usd='1.05', ttl='TTL-MARK')
    assert 'TTL-MARK' in rendered


@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa'])
def test_every_locale_warns_that_the_wallet_account_is_charged(language):
    """AW платит только со счёта: сумма в предупреждении та же, что «К оплате»."""
    template = json.loads((_LOCALES / f'{language}.json').read_text(encoding='utf-8'))['ANTARCTIC_PAYMENT_CREATED']

    rendered = template.format(name='AW', amount='100 ₽', amount_usd='1.05', ttl='TTL')
    assert rendered.count('$1.05') == 2


@pytest.mark.asyncio
async def test_invoice_without_referral_has_no_button_and_no_hint(monkeypatch):
    monkeypatch.setattr(antarctic_handlers, 'PaymentService', _service(_result(referral=None)))
    message = _amount_message()

    await antarctic_handlers.process_antarctic_payment_amount(message, _user(), object(), 10000, _state())

    sent = message.answer.call_args
    assert 'Нет кошелька' not in sent.args[0]
    rows = sent.kwargs['reply_markup'].inline_keyboard
    assert [row[0].callback_data for row in rows[1:]] == ['check_antarctic_7', 'balance_topup']
    assert all(button.url in (None, APP_LINK) for row in rows for button in row)


@pytest.mark.asyncio
async def test_creation_failure_reports_error(monkeypatch):
    monkeypatch.setattr(antarctic_handlers, 'PaymentService', _service(None))
    message = _amount_message()

    await antarctic_handlers.process_antarctic_payment_amount(message, _user(), object(), 10000, _state())

    assert 'Не удалось создать платёж Antarctic Wallet' in message.answer.call_args.args[0]


@pytest.mark.asyncio
async def test_amount_below_minimum_is_refused(monkeypatch):
    monkeypatch.setattr(antarctic_handlers, 'PaymentService', _service(_result()))
    message = _amount_message()

    await antarctic_handlers.process_antarctic_payment_amount(message, _user(), object(), 100, _state())

    assert antarctic_handlers.PaymentService.calls == []
    message.answer.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('payload', 'expected'),
    [
        ({'payment': SimpleNamespace(user=_user()), 'status': 'pending', 'is_paid': False}, '⏳ Ожидает оплаты'),
        ({'payment': SimpleNamespace(user=_user()), 'status': 'success', 'is_paid': True}, '✅ Платёж зачислен'),
        (
            {'payment': SimpleNamespace(user=_user()), 'status': 'expired', 'is_paid': False},
            '❌ Платёж не прошёл или истёк',
        ),
    ],
)
async def test_check_button_answers_with_status(monkeypatch, payload, expected):
    calls = []

    class _Service:
        def __init__(self, _bot=None):
            pass

        async def get_antarctic_payment_status(self, db, local_id):
            calls.append((db, local_id))
            return payload

    monkeypatch.setattr(antarctic_handlers, 'PaymentService', _Service)
    callback = _callback('check_antarctic_7')
    db = object()

    await antarctic_handlers.check_antarctic_payment_status(callback, db)

    assert calls == [(db, 7)]
    assert callback.answer.call_args.args[0] == expected


@pytest.mark.asyncio
async def test_amount_message_is_routed_to_antarctic(monkeypatch):
    from app.handlers.balance import main as balance_main

    process = AsyncMock()
    monkeypatch.setattr(antarctic_handlers, 'process_antarctic_payment_amount', process)
    session = MagicMock()
    session.return_value.__aenter__ = AsyncMock(return_value=object())
    session.return_value.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr('app.database.database.AsyncSessionLocal', session)

    handled = await balance_main.route_payment_by_method(_amount_message(), _user(), 10000, _state(), 'antarctic')

    assert handled is True
    process.assert_awaited_once()
