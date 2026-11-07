"""Кнопки карточки ассистента поддержки: «Отправить», «Править», «Пропустить».

Карточка лежит в топике тикетов группового админ-чата (``ChatTypeFilterMiddleware`` пропускает туда
только кнопки из ``app.keyboards.group_callbacks``), а FSM-ввод в группе невозможен. Поэтому:

* «Отправить» и «Пропустить» работают прямо в группе;
* «Править» присылает нажавшему админу в личку черновик и ставит ``SupportAssistStates.waiting_edit``
  в ЕГО личном FSM (ключ ``chat_id = user_id``). Следующий текст админа в личке уходит клиенту ответом,
  после чего сообщение карточки в группе правится на строку решения. Если карточка лежит в личке админа,
  тот же путь работает без смены чата.

Одна правка на админа: второе «Править» заменяет первое, а старый запрос в личке переписывается на
«заменён» и теряет «Отмену». «Отмена» несёт номер карточки (``sa:cancel:<id>``) и снимает ожидание только
своей правки; текст ответом на другой запрос не принимается. Так ответ, написанный для одного клиента, не уходит
другому (отправку не отозвать).

Запрос правки живёт ``EDIT_TTL_SECONDS`` (30 минут) от нажатия: позже текст не отправляется, состояние снимается,
карточка остаётся 'shown'. Текст, начинающийся с «/», клиенту не уходит никогда: это команда, а не ответ.

«Отправить» шлёт клиенту черновик как есть, поэтому работает только для черновика, который поместился в карточку
целиком (``render_card_checked``): урезанного текста владелец не видел. Для длинного черновика остаётся «Править».

Отправка и там, и там — ``send_admin_reply`` из ``tickets.py``, та же последовательность, что у ручного
ответа. Гонку нажатий решает ``claim_card_decision`` (один условный UPDATE): клиент получает ровно одно
сообщение. Если запись ответа упала после захвата, карточка возвращается в 'shown'.
"""

import contextlib
import html
import time
from dataclasses import dataclass
from enum import StrEnum

import structlog
from aiogram import Bot, Dispatcher, F, types
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.support_assist import claim_card_decision, get_card, release_card_decision
from app.database.crud.ticket import TicketCRUD
from app.database.models import TicketStatus
from app.handlers.admin.tickets import send_admin_reply
from app.services.support_assist.card import Card
from app.services.support_assist.flags import Flag
from app.services.support_assist.render import TELEGRAM_LIMIT, render_card, render_card_checked
from app.services.support_settings_service import SupportSettingsService
from app.states import SupportAssistStates
from app.utils.ticket_text import TICKET_MESSAGE_MAX_LENGTH


logger = structlog.get_logger(__name__)

PREFIX_SEND = 'sa:send:'
PREFIX_EDIT = 'sa:edit:'
PREFIX_SKIP = 'sa:skip:'
PREFIX_CANCEL = 'sa:cancel:'

NO_ACCESS = 'Нет доступа'
SEND_DISABLED = 'Отправка из карточки выключена'
STALE_BUTTON = 'Кнопка устарела'
STALE_PROMPT = 'Запрос правки устарел.'
EDIT_EXPIRED = (
    'Время правки истекло (больше 30 минут), текст не отправлен. '
    'Нажмите «Править» под карточкой ещё раз: карточка не тронута.'
)
COMMAND_NOT_REPLY = (
    'Команда клиенту не отправляется. Пришлите ответ обычным текстом (не с «/» в начале) '
    'или нажмите «Отмена» под черновиком.'
)
FOREIGN_REPLY = (
    'Это ответ на другое сообщение, текст не отправлен. Пришлите его обычным сообщением '
    'или ответом на последний запрос «Правка ответа по тикету».'
)

# Ключи данных личного FSM: какую карточку правим и где лежит её сообщение (его потом правим на строку решения).
KEY_CARD_ID = 'sa_card_id'
KEY_CHAT_ID = 'sa_chat_id'
KEY_MESSAGE_ID = 'sa_message_id'
# Сообщение-запрос правки в личке (на нём «Отмена»): по нему отличаем актуальный запрос от заменённого.
KEY_PROMPT_ID = 'sa_prompt_id'
# Когда админ нажал «Править» (секунды с начала эпохи): по нему запрос правки протухает.
KEY_STARTED_AT = 'edit_started_at'

# Сколько запрос правки ждёт текст. Дольше — риск, что админ уже забыл, о каком клиенте речь.
EDIT_TTL_SECONDS = 30 * 60
# Допуск на расхождение часов: время старта из будущего дальше этого считается порчей данных.
CLOCK_SKEW_SECONDS = 60

# Потолок текста сообщения Telegram и запас на обвязку запроса правки (заголовок, подсказка).
PROMPT_LIMIT = 4096
PROMPT_OVERHEAD = 400

DECISION_LINE = {
    'sent': '✅ отправлено',
    'edited': '✅ отправлено (текст изменён)',
    'dismissed': '⏭ пропущено',
}
# У карточки без модели статус 'fallback' и единственная кнопка «Пропустить».
SKIPPABLE_FROM = ('shown', 'fallback')

_NO_KEYBOARD = types.InlineKeyboardMarkup(inline_keyboard=[])


class Verdict(StrEnum):
    OK = 'ok'
    ALREADY = 'already'
    NO_CARD = 'no_card'
    NO_TICKET = 'no_ticket'
    CLOSED = 'closed'
    NO_TEXT = 'no_text'
    TOO_LONG = 'too_long'
    DRAFT_NOT_SHOWN = 'draft_not_shown'
    FAILED = 'failed'


_VERDICT_TEXT = {
    Verdict.ALREADY: 'Уже обработано',
    Verdict.NO_CARD: 'Карточка не найдена',
    Verdict.NO_TICKET: 'Тикет не найден',
    Verdict.CLOSED: 'Тикет закрыт, ответ не отправлен. Нажмите «Пропустить» или откройте тикет вручную.',
    Verdict.NO_TEXT: 'В карточке нет черновика',
    Verdict.TOO_LONG: f'Текст длиннее {TICKET_MESSAGE_MAX_LENGTH} знаков: нажмите «Править» и сократите его',
    Verdict.DRAFT_NOT_SHOWN: (
        'Черновик не поместился в карточку целиком, владелец его не видел: нажмите «Править» и пришлите текст'
    ),
    Verdict.FAILED: 'Не удалось отправить, карточка осталась на месте. Попробуйте ещё раз.',
}


@dataclass(frozen=True, slots=True)
class _CardView:
    """Простые значения карточки, снятые сразу после чтения.

    Откат сессии (неудачная запись ответа) протухает все загруженные ORM-строки, а чтение протухшего
    атрибута в async-сессии бросает MissingGreenlet; дальше работаем только с этими копиями.
    """

    id: int
    ticket_id: int
    status: str
    draft: str
    card: dict | None
    flags: list[dict]


@dataclass(frozen=True, slots=True)
class _Decision:
    verdict: Verdict
    card: _CardView | None = None


# --- общие помощники -----------------------------------------------------------------------------------------------


def _refusal(user_id: int, *, needs_send: bool) -> str | None:
    """Почему нажатие нельзя выполнять (None — можно)."""
    if not (settings.is_admin(user_id) or SupportSettingsService.is_moderator(user_id)):
        return NO_ACCESS
    if needs_send and not settings.SUPPORT_ASSIST_SEND_BUTTONS_ENABLED:
        return SEND_DISABLED
    return None


def _card_id(data: str | None, prefix: str) -> int | None:
    if not data or not data.startswith(prefix):
        return None
    raw = data[len(prefix) :]
    return int(raw) if raw.isdecimal() else None


async def _load_view(db: AsyncSession, card_id: int) -> _CardView | None:
    row = await get_card(db, card_id)
    if row is None:
        return None
    card = row.card if isinstance(row.card, dict) else None
    return _CardView(
        id=row.id,
        ticket_id=row.ticket_id,
        status=row.status,
        draft=((card or {}).get('draft') or '').strip(),
        card=card,
        flags=list(row.flags or []),
    )


def _private_state(state: FSMContext, user_id: int) -> FSMContext:
    """Личный FSM админа (``chat_id = user_id``), с какого бы чата ни пришло нажатие.

    Ключ собран по стратегии USER_IN_CHAT, которой живёт диспетчер бота: для лички она даёт
    ``(chat_id=user_id, user_id=user_id)`` без топика, как у обычного сообщения админа в личке.
    """
    key = state.key
    return FSMContext(
        storage=state.storage,
        key=StorageKey(bot_id=key.bot_id, chat_id=user_id, user_id=user_id, destiny=key.destiny),
    )


def _card_text(view: _CardView, line: str) -> str | None:
    """Карточка в том виде, как её отправил конвейер, плюс строка решения; в 4096 знаков не больше."""
    suffix = f'\n\n{line}'
    try:
        card = Card.model_validate(view.card) if view.card else None
        flags = [Flag(**f) for f in view.flags]
        return render_card(card, flags, view.ticket_id, limit=TELEGRAM_LIMIT - len(suffix)) + suffix
    except Exception:
        logger.warning('support_assist.card_text_rebuild_failed', card_id=view.id, exc_info=True)
        return None


def _draft_fully_shown(view: _CardView) -> bool:
    """Весь ли черновик был в сообщении карточки (карточка собирается так же, как её собрал конвейер).

    Не удалось собрать карточку заново — доказать нечем, считаем, что не весь.
    """
    try:
        card = Card.model_validate(view.card)
        flags = [Flag(**f) for f in view.flags]
        return not render_card_checked(card, flags, view.ticket_id).draft_truncated
    except Exception:
        logger.warning('support_assist.draft_visibility_check_failed', card_id=view.id, exc_info=True)
        return False


async def _mark_card_message(bot: Bot, view: _CardView, chat_id: int, message_id: int, status: str) -> None:
    """Снять кнопки с карточки и дописать, что решено. Сбой правки решения не отменяет."""
    text = _card_text(view, DECISION_LINE[status])
    try:
        if text is not None:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                parse_mode='HTML',
                disable_web_page_preview=True,
                reply_markup=_NO_KEYBOARD,
            )
            return
    except TelegramAPIError:
        logger.warning('support_assist.card_edit_failed', card_id=view.id, chat_id=chat_id, exc_info=True)
    # Текст не собрался или не правится (сообщение старое, удалено): хотя бы снимаем кнопки.
    try:
        await bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=None)
    except TelegramAPIError:
        logger.warning('support_assist.card_keyboard_removal_failed', card_id=view.id, chat_id=chat_id, exc_info=True)


async def _decide_and_send(
    bot: Bot,
    db: AsyncSession,
    *,
    card_id: int,
    admin_telegram_id: int,
    admin_db_id: int,
    to_status: str,
    text: str | None,
) -> _Decision:
    """Занять карточку и отправить клиенту ответ (``text`` или черновик карточки)."""
    view = await _load_view(db, card_id)
    if view is None:
        return _Decision(Verdict.NO_CARD)
    if view.status != 'shown':
        return _Decision(Verdict.ALREADY, view)

    body = (view.draft if text is None else text).strip()
    if not body:
        return _Decision(Verdict.NO_TEXT, view)
    if len(body) > TICKET_MESSAGE_MAX_LENGTH:
        return _Decision(Verdict.TOO_LONG, view)
    # Черновик уходит клиенту как есть: он должен был целиком лежать в карточке. Свой текст админ видел сам.
    if text is None and not _draft_fully_shown(view):
        return _Decision(Verdict.DRAFT_NOT_SHOWN, view)

    ticket = await TicketCRUD.get_ticket_by_id(db, view.ticket_id, load_messages=False, load_user=True)
    if ticket is None:
        return _Decision(Verdict.NO_TICKET, view)
    # Ручной ответ в закрытый тикет админке недоступен, а add_message переоткрыл бы его как ANSWERED.
    if ticket.status == TicketStatus.CLOSED.value:
        return _Decision(Verdict.CLOSED, view)

    # Занимаем карточку ДО ответа: проигравший второе нажатие ничего не отправляет.
    if not await claim_card_decision(db, card_id, to_status, admin_telegram_id, final_text=body):
        return _Decision(Verdict.ALREADY, view)

    try:
        await send_admin_reply(bot, db, ticket, admin_db_id, body)
    except Exception:
        logger.exception('support_assist.reply_failed', card_id=card_id, ticket_id=view.ticket_id)
        # Сессия после сбоя может быть в сбойном состоянии: откат, затем возврат карточки в 'shown'.
        with contextlib.suppress(Exception):
            await db.rollback()
        try:
            await release_card_decision(db, card_id, to_status, admin_telegram_id)
        except Exception:
            logger.exception('support_assist.release_failed', card_id=card_id)
        return _Decision(Verdict.FAILED, view)

    logger.info(
        'support_assist.decided',
        card_id=card_id,
        ticket_id=view.ticket_id,
        status=to_status,
        decided_by=admin_telegram_id,
    )
    return _Decision(Verdict.OK, view)


# --- «Отправить» ---------------------------------------------------------------------------------------------------


async def handle_send(callback: types.CallbackQuery, bot: Bot, db: AsyncSession, db_user):
    refusal = _refusal(callback.from_user.id, needs_send=True)
    if refusal or db_user is None:
        await callback.answer(refusal or NO_ACCESS, show_alert=True)
        return
    card_id = _card_id(callback.data, PREFIX_SEND)
    if card_id is None:
        await callback.answer(STALE_BUTTON, show_alert=True)
        return

    decision = await _decide_and_send(
        bot,
        db,
        card_id=card_id,
        admin_telegram_id=callback.from_user.id,
        admin_db_id=db_user.id,
        to_status='sent',
        text=None,
    )
    if decision.verdict is not Verdict.OK:
        await callback.answer(_VERDICT_TEXT[decision.verdict], show_alert=True)
        return

    message = callback.message
    if message is not None and decision.card is not None:
        await _mark_card_message(bot, decision.card, message.chat.id, message.message_id, 'sent')
    await callback.answer('Отправлено')


# --- «Править» ------------------------------------------------------------------------------------------------------


def _fit_escaped(text: str, budget: int) -> str:
    """Экранированный для HTML текст не длиннее ``budget``; режем исходник, а не готовую разметку."""
    keep = len(text)
    escaped = html.escape(text, quote=False)
    while len(escaped) > budget and keep > 0:
        keep = int(keep * 0.9)
        escaped = html.escape(text[:keep].rstrip(), quote=False) + '…'
    return escaped


def _edit_prompt(view: _CardView) -> str:
    draft = _fit_escaped(view.draft, PROMPT_LIMIT - PROMPT_OVERHEAD)
    return (
        f'✏️ <b>Правка ответа по тикету #{view.ticket_id}</b>\n\n'
        f'Черновик:\n{draft}\n\n'
        'Пришлите одним сообщением текст, который нужно отправить клиенту. '
        'Он уйдёт сразу и без изменений. Запрос действует 30 минут.'
    )


def _cancel_keyboard(card_id: int) -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text='Отмена', callback_data=f'{PREFIX_CANCEL}{card_id}')]]
    )


async def _retire_pending_prompt(bot: Bot, private: FSMContext, user_id: int, new_ticket_id: int) -> None:
    """Прежний запрос правки (если ждём текст) переписать на «заменён» и снять с него «Отмену».

    Сбой правки сообщения не мешает новой правке: устаревший запрос всё равно не примет ни «Отмену»
    (номер карточки не тот), ни текст ответом на него (номер сообщения не тот).
    """
    if await private.get_state() != SupportAssistStates.waiting_edit.state:
        return
    old_prompt_id = (await private.get_data()).get(KEY_PROMPT_ID)
    if not isinstance(old_prompt_id, int):
        return
    try:
        await bot.edit_message_text(
            chat_id=user_id,
            message_id=old_prompt_id,
            text=f'⏭ Запрос заменён: теперь жду ответ по тикету #{new_ticket_id}.',
            reply_markup=_NO_KEYBOARD,
        )
    except TelegramAPIError:
        logger.warning('support_assist.prompt_retire_failed', chat_id=user_id, message_id=old_prompt_id, exc_info=True)


async def handle_edit(callback: types.CallbackQuery, state: FSMContext, bot: Bot, db: AsyncSession):
    user_id = callback.from_user.id
    refusal = _refusal(user_id, needs_send=True)
    if refusal:
        await callback.answer(refusal, show_alert=True)
        return
    card_id = _card_id(callback.data, PREFIX_EDIT)
    message = callback.message
    if card_id is None or message is None:
        await callback.answer(STALE_BUTTON, show_alert=True)
        return

    view = await _load_view(db, card_id)
    if view is None:
        await callback.answer(_VERDICT_TEXT[Verdict.NO_CARD], show_alert=True)
        return
    if view.status != 'shown':
        await callback.answer(_VERDICT_TEXT[Verdict.ALREADY], show_alert=True)
        return

    # Сначала личка: если админ ни разу не открывал бота, Telegram не даст написать, и состояние-ловушку
    # ставить не нужно. Карточка при этом остаётся 'shown'.
    try:
        prompt = await bot.send_message(
            chat_id=user_id,
            text=_edit_prompt(view),
            parse_mode='HTML',
            reply_markup=_cancel_keyboard(card_id),
        )
    except TelegramForbiddenError:
        await callback.answer(
            'Не могу написать вам в личные сообщения. Откройте бота, нажмите /start и повторите «Править».',
            show_alert=True,
        )
        return
    except TelegramAPIError:
        logger.warning('support_assist.edit_prompt_failed', card_id=card_id, exc_info=True)
        await callback.answer('Не удалось написать вам в личные сообщения. Попробуйте ещё раз.', show_alert=True)
        return

    private = _private_state(state, user_id)
    # Одна правка на админа: прежний запрос закрываем до смены состояния, чтобы он не выглядел живым.
    await _retire_pending_prompt(bot, private, user_id, view.ticket_id)
    await private.set_state(SupportAssistStates.waiting_edit)
    await private.set_data(
        {
            KEY_CARD_ID: card_id,
            KEY_CHAT_ID: message.chat.id,
            KEY_MESSAGE_ID: message.message_id,
            KEY_PROMPT_ID: getattr(prompt, 'message_id', None),
            KEY_STARTED_AT: time.time(),
        },
    )
    in_private = getattr(message.chat, 'type', None) == 'private'
    await callback.answer(None if in_private else 'Черновик отправлен вам в личные сообщения')


def _edit_expired(data: dict) -> bool:
    """Запрос правки протух (или не доказать, что свежий): нет времени старта, оно мусорное или из далёкого будущего."""
    started = data.get(KEY_STARTED_AT)
    if isinstance(started, bool) or not isinstance(started, int | float):
        return True
    age = time.time() - started
    return not (-CLOCK_SKEW_SECONDS <= age <= EDIT_TTL_SECONDS)


async def handle_edit_text(message: types.Message, state: FSMContext, bot: Bot, db: AsyncSession, db_user):
    refusal = _refusal(message.from_user.id, needs_send=True)
    if refusal or db_user is None:
        await state.clear()
        await message.answer(refusal or NO_ACCESS)
        return

    data = await state.get_data()
    card_id = data.get(KEY_CARD_ID)
    if not isinstance(card_id, int):
        await state.clear()
        await message.answer(STALE_BUTTON)
        return

    if _edit_expired(data):
        await state.clear()
        await message.answer(EDIT_EXPIRED)
        return

    # Ответом (reply) на запрос, который уже заменён, текст не принимаем: он мог быть написан для другого клиента.
    prompt_id = data.get(KEY_PROMPT_ID)
    replied_to = getattr(getattr(message, 'reply_to_message', None), 'message_id', None)
    if isinstance(prompt_id, int) and isinstance(replied_to, int) and replied_to != prompt_id:
        await message.answer(FOREIGN_REPLY)
        return

    text = (message.text or '').strip()
    if not text:
        await message.answer('Пришлите ответ текстом или нажмите «Отмена» под черновиком.')
        return
    if text.startswith('/'):
        # Команда, а не ответ: клиенту её слать нельзя. Состояние остаётся, админ пришлёт обычный текст.
        await message.answer(COMMAND_NOT_REPLY)
        return
    if len(text) > TICKET_MESSAGE_MAX_LENGTH:
        await message.answer(
            f'Ответ слишком длинный: максимум {TICKET_MESSAGE_MAX_LENGTH} знаков. Сократите и пришлите ещё раз.'
        )
        return

    decision = await _decide_and_send(
        bot,
        db,
        card_id=card_id,
        admin_telegram_id=message.from_user.id,
        admin_db_id=db_user.id,
        to_status='edited',
        text=text,
    )
    if decision.verdict is Verdict.FAILED:
        # Состояние сохраняем: админ может прислать текст ещё раз, карточка свободна.
        await message.answer(_VERDICT_TEXT[Verdict.FAILED])
        return
    await state.clear()
    if decision.verdict is not Verdict.OK:
        await message.answer(_VERDICT_TEXT[decision.verdict])
        return

    chat_id, message_id = data.get(KEY_CHAT_ID), data.get(KEY_MESSAGE_ID)
    if decision.card is not None and isinstance(chat_id, int) and isinstance(message_id, int):
        await _mark_card_message(bot, decision.card, chat_id, message_id, 'edited')
    ticket_id = decision.card.ticket_id if decision.card is not None else ''
    await message.answer(f'✅ Ответ по тикету #{ticket_id} отправлен клиенту.')


async def _is_current_prompt(callback: types.CallbackQuery, state: FSMContext) -> bool:
    """«Отмена» относится к правке, которую админ ждёт сейчас (тот же номер карточки и тот же запрос)."""
    card_id = _card_id(callback.data, PREFIX_CANCEL)
    if card_id is None or await state.get_state() != SupportAssistStates.waiting_edit.state:
        return False
    data = await state.get_data()
    if data.get(KEY_CARD_ID) != card_id:
        return False
    prompt_id = data.get(KEY_PROMPT_ID)
    message_id = getattr(callback.message, 'message_id', None)
    return not (isinstance(prompt_id, int) and isinstance(message_id, int) and prompt_id != message_id)


async def handle_cancel(callback: types.CallbackQuery, state: FSMContext):
    """«Отмена» под запросом правки (только в личке): снять ожидание текста, но только своей правки."""
    current = await _is_current_prompt(callback, state)
    if current:
        await state.clear()
    message = callback.message
    if message is not None and hasattr(message, 'edit_text'):
        with contextlib.suppress(TelegramAPIError):
            await message.edit_text('Правка отменена.' if current else STALE_PROMPT, reply_markup=None)
    await callback.answer()


# --- «Пропустить» ---------------------------------------------------------------------------------------------------


async def handle_skip(callback: types.CallbackQuery, bot: Bot, db: AsyncSession):
    refusal = _refusal(callback.from_user.id, needs_send=False)
    if refusal:
        await callback.answer(refusal, show_alert=True)
        return
    card_id = _card_id(callback.data, PREFIX_SKIP)
    if card_id is None:
        await callback.answer(STALE_BUTTON, show_alert=True)
        return

    view = await _load_view(db, card_id)
    if view is None:
        await callback.answer(_VERDICT_TEXT[Verdict.NO_CARD], show_alert=True)
        return

    admin_id = callback.from_user.id
    claimed = False
    for from_status in SKIPPABLE_FROM:
        if await claim_card_decision(db, card_id, 'dismissed', admin_id, from_status=from_status):
            claimed = True
            break
    if not claimed:
        await callback.answer(_VERDICT_TEXT[Verdict.ALREADY], show_alert=True)
        return

    logger.info(
        'support_assist.decided', card_id=card_id, ticket_id=view.ticket_id, status='dismissed', decided_by=admin_id
    )
    message = callback.message
    if message is not None:
        await _mark_card_message(bot, view, message.chat.id, message.message_id, 'dismissed')
    await callback.answer('Пропущено')


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(handle_send, F.data.startswith(PREFIX_SEND))
    dp.callback_query.register(handle_edit, F.data.startswith(PREFIX_EDIT))
    dp.callback_query.register(handle_skip, F.data.startswith(PREFIX_SKIP))
    # «Отмена» живёт только в личке админа: в списке групповых кнопок её нет.
    dp.callback_query.register(handle_cancel, F.data.startswith(PREFIX_CANCEL))
    dp.message.register(handle_edit_text, SupportAssistStates.waiting_edit)
