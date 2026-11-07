import asyncio
import html
import time

import structlog
from aiogram import Bot, Dispatcher, F, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InaccessibleMessage
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.ticket import TicketCRUD, TicketMessageCRUD
from app.database.crud.user import get_user_by_id
from app.database.models import Ticket, TicketStatus, User
from app.keyboards.inline import (
    get_my_tickets_keyboard,
    get_ticket_cancel_keyboard,
    get_ticket_notification_keyboard,
    get_ticket_reply_cancel_keyboard,
    get_ticket_view_keyboard,
)
from app.localization.texts import get_texts
from app.services.admin_notification_service import AdminNotificationService
from app.services.support_assist.form_options import FormOptions, get_form_options
from app.services.support_assist.pipeline import schedule_assist
from app.services.support_assist.ticket_form import (
    FORM_FIELD_IDS,
    OTHER_MAX_LENGTH,
    OTHER_OPTION,
    STATIC_OPTIONS,
    answered_fields,
    clean_other_text,
    has_other,
    keep_valid_answers,
    other_key,
    platform_label,
    render_diagnostics,
    validate_diagnostics,
)
from app.utils.cache import RateLimitCache, cache, cache_key
from app.utils.formatters import format_username_link
from app.utils.miniapp_buttons import build_admin_ticket_cabinet_button
from app.utils.photo_message import edit_or_answer_photo, safe_edit_or_resend
from app.utils.ticket_media import get_ticket_media, ticket_media_chunks
from app.utils.ticket_text import (
    TICKET_MESSAGE_MAX_LENGTH,
    TICKET_PAGE_MAX_LEN,
    build_ticket_pages,
    preview_text,
)
from app.utils.timezone import format_local_datetime


logger = structlog.get_logger(__name__)


class TicketStates(StatesGroup):
    waiting_for_title = State()
    waiting_for_message = State()
    waiting_for_reply = State()
    # Обязательная анкета перед заголовком (SUPPORT_TICKET_FORM_REQUIRED): вопросы кнопками,
    # текст принимается только в «Другое».
    waiting_for_form = State()
    waiting_for_form_other = State()


async def show_ticket_priority_selection(
    callback: types.CallbackQuery, state: FSMContext, db_user: User, db: AsyncSession
):
    """Начать создание тикета без выбора приоритета: сразу просим заголовок"""
    texts = get_texts(db_user.language)

    # Глобальный блок и наличие активного тикета
    from app.database.crud.ticket import TicketCRUD

    blocked_until = await TicketCRUD.is_user_globally_blocked(db, db_user.id)
    if blocked_until:
        if blocked_until.year > 9999 - 1:
            await callback.answer(
                texts.t('USER_BLOCKED_FOREVER', 'Вы заблокированы для обращений в поддержку.'), show_alert=True
            )
        else:
            await callback.answer(
                texts.t('USER_BLOCKED_UNTIL', 'Вы заблокированы до {time}').format(
                    time=format_local_datetime(blocked_until, '%d.%m.%Y %H:%M')
                ),
                show_alert=True,
            )
        return
    if await TicketCRUD.user_has_active_ticket(db, db_user.id):
        await callback.answer(
            texts.t('TICKET_ALREADY_OPEN', 'У вас уже есть незакрытый тикет. Сначала закройте его.'), show_alert=True
        )
        return

    if settings.SUPPORT_TICKET_FORM_REQUIRED:
        await _start_ticket_form(callback, state, db_user)
        return

    prompt_text = texts.t('TICKET_TITLE_INPUT', 'Введите заголовок тикета:')
    cancel_kb = get_ticket_cancel_keyboard(db_user.language)
    prompt_msg = callback.message
    try:
        await callback.message.edit_text(prompt_text, reply_markup=cancel_kb)
    except TelegramBadRequest:
        # Предыдущее сообщение — фото (нет текста для edit_text), удаляем и шлём новое
        try:
            await callback.message.delete()
        except Exception:
            pass
        prompt_msg = await callback.message.answer(prompt_text, reply_markup=cancel_kb)
    # Запоминаем исходное сообщение бота, чтобы далее редактировать его, а не слать новые
    await state.update_data(prompt_chat_id=prompt_msg.chat.id, prompt_message_id=prompt_msg.message_id)
    await state.set_state(TicketStates.waiting_for_title)
    await callback.answer()


def _next_form_field(diagnostics: dict | None) -> str | None:
    """Первое поле анкеты без ответа; ``None`` — анкета заполнена."""
    done = answered_fields(diagnostics)
    return next((field for field in FORM_FIELD_IDS if field not in done), None)


def _form_choices(field: str, language: str, diagnostics: dict, options: FormOptions) -> list[tuple[str, str]]:
    """Кнопки вопроса: (текст, значение ответа)."""
    texts = get_texts(language)
    if field == 'platform':
        return [(platform_label(pid, language, options), pid) for pid in options.platform_ids()]
    if field == 'app':
        return [(name, name) for name in options.apps_for(diagnostics.get('platform', ''))]
    return [(texts.t(f'TICKET_FORM_{field.upper()}_{option.upper()}'), option) for option in STATIC_OPTIONS[field]]


# Устройство и приложение приходят из конфига Remnawave, названия бывают длинными: в callback_data
# (до 64 байт) кладём номер кнопки, а показанный список сохраняем в состоянии (``form_choices``).
# Нажатие разбираем по этому снимку: конфиг мог смениться, пока экран был открыт.
_INDEXED_FIELDS = ('platform', 'app')


def _is_index(raw: str, size: int) -> bool:
    return raw.isascii() and raw.isdigit() and int(raw) < size


def _resolve_choice(field: str, raw: str, shown: list | None) -> str | None:
    """Значение ответа по нажатой кнопке; ``None`` — кнопка не из показанного списка."""
    if field in _INDEXED_FIELDS:
        if field == 'app' and raw == OTHER_OPTION:
            return OTHER_OPTION
        if not isinstance(shown, list) or not _is_index(raw, len(shown)):
            return None
        value = shown[int(raw)]
        return value if isinstance(value, str) else None
    return raw if raw in STATIC_OPTIONS.get(field, ()) else None


def _form_keyboard(field: str, choices: list[tuple[str, str]], language: str) -> types.InlineKeyboardMarkup:
    texts = get_texts(language)
    indexed = field in _INDEXED_FIELDS
    rows = [
        [types.InlineKeyboardButton(text=label, callback_data=f'tf:{field}:{i if indexed else value}')]
        for i, (label, value) in enumerate(choices)
    ]
    if field == 'app':
        rows.append(
            [types.InlineKeyboardButton(text=texts.t('TICKET_FORM_APP_OTHER'), callback_data=f'tf:app:{OTHER_OPTION}')]
        )
    rows.append(
        [
            types.InlineKeyboardButton(
                text=texts.t('CANCEL_TICKET_CREATION', '❌ Отменить создание тикета'),
                callback_data='cancel_ticket_creation',
            )
        ]
    )
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


async def _form_screen(
    state: FSMContext, language: str, diagnostics: dict | None
) -> tuple[str, types.InlineKeyboardMarkup, State]:
    """Следующий экран: очередной вопрос анкеты, а когда ответы собраны, обычный запрос заголовка.

    Показанный список устройств или приложений запоминается в состоянии (``form_choices``).
    """
    texts = get_texts(language)
    field = _next_form_field(diagnostics)
    if field is None:
        return (
            texts.t('TICKET_TITLE_INPUT', 'Введите заголовок тикета:'),
            get_ticket_cancel_keyboard(language),
            TicketStates.waiting_for_title,
        )
    choices = _form_choices(field, language, diagnostics or {}, await get_form_options())
    await state.update_data(form_choices=[value for _, value in choices] if field in _INDEXED_FIELDS else None)
    step = FORM_FIELD_IDS.index(field) + 1
    text = (
        f'{texts.t("TICKET_FORM_STEP").format(step=step, total=len(FORM_FIELD_IDS))}\n\n'
        f'{texts.t(f"TICKET_FORM_{field.upper()}_PROMPT")}'
    )
    return text, _form_keyboard(field, choices, language), TicketStates.waiting_for_form


async def _edit_form_prompt(
    callback: types.CallbackQuery, state: FSMContext, text: str, keyboard: types.InlineKeyboardMarkup
) -> None:
    """Показать экран анкеты в сообщении с кнопкой; не вышло (например, там фото) — отправить новое."""
    prompt_msg = callback.message
    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
    except TelegramBadRequest:
        # Предыдущее сообщение — фото (нет текста для edit_text), удаляем и шлём новое
        try:
            await callback.message.delete()
        except Exception:
            pass
        prompt_msg = await callback.message.answer(text, reply_markup=keyboard)
    # Дальше редактируем именно это сообщение, а не шлём новые (как в обычном потоке тикета).
    await state.update_data(prompt_chat_id=prompt_msg.chat.id, prompt_message_id=prompt_msg.message_id)


async def _start_ticket_form(callback: types.CallbackQuery, state: FSMContext, db_user: User) -> None:
    """Первый вопрос анкеты вместо запроса заголовка (блокировки и открытый тикет уже проверены)."""
    text, keyboard, next_state = await _form_screen(state, db_user.language, {})
    await _edit_form_prompt(callback, state, text, keyboard)
    await state.update_data(diagnostics={}, form_other_field=None)
    await state.set_state(next_state)
    await callback.answer()


async def handle_ticket_form_answer(
    callback: types.CallbackQuery, state: FSMContext, db_user: User, db: AsyncSession | None = None
):
    """Ответ на вопрос анкеты: кнопка ``tf:<поле>:<вариант>`` (устройство и приложение — номер варианта)."""
    if await state.get_state() != TicketStates.waiting_for_form:
        # Кнопка от прошлого сценария или отменённого тикета.
        await callback.answer()
        return
    parts = (callback.data or '').split(':')
    data = await state.get_data()
    diagnostics = dict(data.get('diagnostics') or {})
    field = _next_form_field(diagnostics)
    # Принимаем только ответ на текущий вопрос: двойной тап или старый экран не пропускают вопросов.
    option = (
        _resolve_choice(field, parts[2], data.get('form_choices'))
        if len(parts) == 3 and field is not None and parts[1] == field
        else None
    )
    if option is None:
        await callback.answer()
        return
    language = db_user.language

    if option == OTHER_OPTION and has_other(field):
        await state.update_data(form_other_field=field)
        await state.set_state(TicketStates.waiting_for_form_other)
        await _edit_form_prompt(
            callback,
            state,
            get_texts(language).t('TICKET_FORM_OTHER_PROMPT').format(limit=OTHER_MAX_LENGTH),
            get_ticket_cancel_keyboard(language),
        )
        await callback.answer()
        return

    diagnostics[field] = option
    diagnostics = await _advance_ticket_form(state, diagnostics)
    if await _finish_pending_ticket(state, db_user, db, diagnostics, _bot_of(callback), callback.message.answer):
        await callback.answer()
        return
    text, keyboard, next_state = await _form_screen(state, language, diagnostics)
    await state.set_state(next_state)
    await _edit_form_prompt(callback, state, text, keyboard)
    await callback.answer()


def _bot_of(event) -> Bot | None:
    return getattr(event, 'bot', None) or getattr(getattr(event, 'message', None), 'bot', None)


async def _advance_ticket_form(state: FSMContext, diagnostics: dict) -> dict:
    """Сохранить ответы; собранную анкету приводим к каноническому виду.

    Негодный сохранённый ответ не роняет сценарий: он отбрасывается, и бот спросит это поле снова.
    """
    if _next_form_field(diagnostics) is None:
        try:
            diagnostics = validate_diagnostics(diagnostics)
        except ValueError as exc:
            logger.warning('Ticket form: invalid saved answer, asking again', error=str(exc))
            diagnostics = keep_valid_answers(diagnostics)
    await state.update_data(diagnostics=diagnostics, form_other_field=None)
    return diagnostics


async def _finish_pending_ticket(
    state: FSMContext, db_user: User, db: AsyncSession | None, diagnostics: dict, bot: Bot | None, answer
) -> bool:
    """Анкета дозаполнена после того, как клиент уже прислал заголовок и текст: создаём тикет из них.

    ``True`` — тикет создан (или попытка сделана), экран анкеты показывать не нужно.
    """
    if _next_form_field(diagnostics) is not None:
        return False
    data = await state.get_data()
    draft = data.get('ticket_draft')
    title = data.get('title')
    if not isinstance(draft, dict) or not title or db is None:
        return False
    await _create_ticket_and_confirm(bot, answer, state, db_user, db, title, draft, validate_diagnostics(diagnostics))
    return True


async def handle_ticket_form_other_input(
    message: types.Message, state: FSMContext, db_user: User, db: AsyncSession | None = None
):
    """Короткий текст к варианту «Другое»."""
    if await state.get_state() != TicketStates.waiting_for_form_other:
        return
    asyncio.create_task(_try_delete_message_later(message.bot, message.chat.id, message.message_id, 2.0))
    data = await state.get_data()
    field = data.get('form_other_field')
    prompt_chat_id = data.get('prompt_chat_id')
    prompt_message_id = data.get('prompt_message_id')
    language = db_user.language

    text = clean_other_text(message.text) if message.text else ''
    # Длинный текст не режем молча (как и текст тикета): просим сократить.
    too_long = bool(message.text) and len(' '.join(message.text.split())) > OTHER_MAX_LENGTH
    if not isinstance(field, str) or not has_other(field) or not text or too_long:
        invalid = get_texts(language).t('TICKET_FORM_OTHER_INVALID').format(limit=OTHER_MAX_LENGTH)
        await _edit_or_send(message, prompt_chat_id, prompt_message_id, invalid, language)
        return

    diagnostics = dict(data.get('diagnostics') or {})
    diagnostics[field] = OTHER_OPTION
    diagnostics[other_key(field)] = text
    diagnostics = await _advance_ticket_form(state, diagnostics)
    if await _finish_pending_ticket(state, db_user, db, diagnostics, message.bot, message.answer):
        return
    screen_text, keyboard, next_state = await _form_screen(state, language, diagnostics)
    await state.set_state(next_state)
    await _edit_or_send(message, prompt_chat_id, prompt_message_id, screen_text, language, reply_markup=keyboard)


async def handle_ticket_form_stray_message(message: types.Message, state: FSMContext):
    """Текст вместо кнопки на вопросе анкеты: вопрос остаётся на экране, лишнее сообщение убираем."""
    if await state.get_state() != TicketStates.waiting_for_form:
        return
    asyncio.create_task(_try_delete_message_later(message.bot, message.chat.id, message.message_id, 2.0))


async def handle_ticket_title_input(message: types.Message, state: FSMContext, db_user: User, db: AsyncSession):
    # Проверяем, что пользователь в правильном состоянии
    current_state = await state.get_state()
    if current_state != TicketStates.waiting_for_title:
        return

    """Обработать ввод заголовка тикета"""
    if not message.text:
        asyncio.create_task(_try_delete_message_later(message.bot, message.chat.id, message.message_id, 2.0))
        return
    title = message.text.strip()

    data_prompt = await state.get_data()
    prompt_chat_id = data_prompt.get('prompt_chat_id')
    prompt_message_id = data_prompt.get('prompt_message_id')
    # Удалим сообщение пользователя через 2 секунды, чтобы не засорять чат
    asyncio.create_task(_try_delete_message_later(message.bot, message.chat.id, message.message_id, 2.0))
    if len(title) < 5:
        texts = get_texts(db_user.language)
        text_val = texts.t(
            'TICKET_TITLE_TOO_SHORT', 'Заголовок должен содержать минимум 5 символов. Попробуйте еще раз:'
        )
        await _edit_or_send(message, prompt_chat_id, prompt_message_id, text_val, db_user.language)
        return

    if len(title) > 255:
        texts = get_texts(db_user.language)
        text_val = texts.t(
            'TICKET_TITLE_TOO_LONG', 'Заголовок слишком длинный. Максимум 255 символов. Попробуйте еще раз:'
        )
        await _edit_or_send(message, prompt_chat_id, prompt_message_id, text_val, db_user.language)
        return

    # Глобальный блок
    from app.database.crud.ticket import TicketCRUD

    blocked_until = await TicketCRUD.is_user_globally_blocked(db, db_user.id)
    if blocked_until:
        texts = get_texts(db_user.language)
        if blocked_until.year > 9999 - 1:
            await message.answer(texts.t('USER_BLOCKED_FOREVER', 'Вы заблокированы для обращений в поддержку.'))
        else:
            await message.answer(
                texts.t('USER_BLOCKED_UNTIL', 'Вы заблокированы до {time}').format(
                    time=format_local_datetime(blocked_until, '%d.%m.%Y %H:%M')
                )
            )
        await state.clear()
        return

    await state.update_data(title=title)

    texts = get_texts(db_user.language)
    text_val = texts.t(
        'TICKET_MESSAGE_INPUT', 'Опишите проблему (до {limit} символов) или отправьте фото с подписью:'
    ).format(limit=TICKET_MESSAGE_MAX_LENGTH)
    await _edit_or_send(message, prompt_chat_id, prompt_message_id, text_val, db_user.language)

    await state.set_state(TicketStates.waiting_for_message)


async def handle_ticket_message_input(message: types.Message, state: FSMContext, db_user: User, db: AsyncSession):
    # Проверяем, что пользователь в правильном состоянии
    current_state = await state.get_state()
    if current_state != TicketStates.waiting_for_message:
        return

    # Защита от спама: принимаем только первое сообщение в коротком окне
    try:
        # Глобальный мягкий супрессор на 6 секунд после создания тикета
        try:
            from_cache = await cache.get(cache_key('suppress_user_input', db_user.id))
            if from_cache:
                asyncio.create_task(_try_delete_message_later(message.bot, message.chat.id, message.message_id, 2.0))
                return
        except Exception:
            pass
        limited = await RateLimitCache.is_rate_limited(db_user.id, 'ticket_create_message', limit=1, window=2)
        if limited:
            # Удаляем лишние части длинного сообщения
            try:
                asyncio.create_task(_try_delete_message_later(message.bot, message.chat.id, message.message_id, 2.0))
            except Exception as exc:
                logger.debug('Антиспам тикетов: сообщение не поставлено на удаление', error=str(exc))
            return
    except Exception:
        pass
    try:
        data_rl = await state.get_data()
        last_ts = data_rl.get('rl_ts_create')
        now_ts = time.time()
        if last_ts and (now_ts - float(last_ts)) < 2:
            try:
                asyncio.create_task(_try_delete_message_later(message.bot, message.chat.id, message.message_id, 2.0))
            except Exception as exc:
                logger.debug('Антиспам тикетов: сообщение не поставлено на удаление', error=str(exc))
            return
        await state.update_data(rl_ts_create=now_ts)
    except Exception:
        pass

    """Обработать ввод сообщения тикета и создать тикет"""
    # Сохраняем вложение отдельно от подписи, в том числе видео без текста.
    message_text = (message.text or message.caption or '').strip()
    media_type = None
    media_file_id = None
    media_caption = None
    if message.photo:
        media_type = 'photo'
        media_file_id = message.photo[-1].file_id
        media_caption = message.caption
    elif message.video:
        media_type = 'video'
        media_file_id = message.video.file_id
        media_caption = message.caption
    # Глобальный блок
    from app.database.crud.ticket import TicketCRUD

    blocked_until = await TicketCRUD.is_user_globally_blocked(db, db_user.id)
    if blocked_until:
        texts = get_texts(db_user.language)
        data_prompt = await state.get_data()
        prompt_chat_id = data_prompt.get('prompt_chat_id')
        prompt_message_id = data_prompt.get('prompt_message_id')
        text_msg = (
            texts.t('USER_BLOCKED_FOREVER', 'Вы заблокированы для обращений в поддержку.')
            if blocked_until.year > 9999 - 1
            else texts.t('USER_BLOCKED_UNTIL', 'Вы заблокированы до {time}').format(
                time=format_local_datetime(blocked_until, '%d.%m.%Y %H:%M')
            )
        )
        if prompt_chat_id and prompt_message_id:
            try:
                await message.bot.edit_message_text(chat_id=prompt_chat_id, message_id=prompt_message_id, text=text_msg)
            except TelegramBadRequest:
                await message.answer(text_msg)
        else:
            await message.answer(text_msg)
        await state.clear()
        return

    # Удалим сообщение пользователя через 2 секунды
    asyncio.create_task(_try_delete_message_later(message.bot, message.chat.id, message.message_id, 2.0))
    # Вложение может описывать проблему без текстовой подписи.
    if len(message_text) < 10 and not media_file_id:
        texts = get_texts(db_user.language)
        data_prompt = await state.get_data()
        prompt_chat_id = data_prompt.get('prompt_chat_id')
        prompt_message_id = data_prompt.get('prompt_message_id')
        err_text = texts.t(
            'TICKET_MESSAGE_TOO_SHORT', 'Сообщение слишком короткое. Опишите проблему подробнее или отправьте фото:'
        )
        await _edit_or_send(message, prompt_chat_id, prompt_message_id, err_text, db_user.language)
        return

    # Раньше текст молча резался до 500 символов — пользователь и поддержка
    # теряли остаток. Теперь просим сократить, ничего не отбрасывая втихую.
    if len(message_text) > TICKET_MESSAGE_MAX_LENGTH:
        texts = get_texts(db_user.language)
        data_prompt = await state.get_data()
        prompt_chat_id = data_prompt.get('prompt_chat_id')
        prompt_message_id = data_prompt.get('prompt_message_id')
        err_text = texts.t(
            'TICKET_MESSAGE_TOO_LONG',
            'Сообщение слишком длинное. Максимум {limit} символов. Сократите текст и отправьте еще раз:',
        ).format(limit=TICKET_MESSAGE_MAX_LENGTH)
        await _edit_or_send(message, prompt_chat_id, prompt_message_id, err_text, db_user.language)
        return

    data = await state.get_data()
    title = data.get('title')

    draft = {
        'text': message_text,
        'media_type': media_type,
        'media_file_id': media_file_id,
        'media_caption': media_caption,
    }
    diagnostics = None
    try:
        diagnostics = validate_diagnostics(data.get('diagnostics'))
    except ValueError:
        if settings.SUPPORT_TICKET_FORM_REQUIRED:
            # Анкета обязательна, а ответов нет или один негоден (флаг включили посреди сценария,
            # старое состояние). Заголовок и текст не теряем: спрашиваем недостающее и потом создаём тикет.
            kept = keep_valid_answers(data.get('diagnostics'))
            await state.update_data(diagnostics=kept, ticket_draft=draft, form_other_field=None)
            screen_text, keyboard, next_state = await _form_screen(state, db_user.language, kept)
            await state.set_state(next_state)
            await _edit_or_send(
                message,
                data.get('prompt_chat_id'),
                data.get('prompt_message_id'),
                screen_text,
                db_user.language,
                reply_markup=keyboard,
            )
            return

    await _create_ticket_and_confirm(message.bot, message.answer, state, db_user, db, title, draft, diagnostics)


async def _create_ticket_and_confirm(
    bot: Bot | None,
    answer,
    state: FSMContext,
    db_user: User,
    db: AsyncSession,
    title: str,
    draft: dict,
    diagnostics: dict | None,
) -> None:
    """Создать тикет из заголовка и черновика сообщения, показать подтверждение, уведомить админов."""
    priority = 'normal'
    message_text = draft.get('text') or ''
    media_type = draft.get('media_type')
    media_file_id = draft.get('media_file_id')
    media_caption = draft.get('media_caption')
    try:
        ticket = await TicketCRUD.create_ticket(
            db,
            db_user.id,
            title,
            message_text,
            priority,
            media_type=media_type,
            media_file_id=media_file_id,
            media_caption=media_caption,
            diagnostics=diagnostics,
        )
        # Включим временное подавление лишних сообщений пользователя (на случай разбиения длинного текста)
        try:
            await cache.set(cache_key('suppress_user_input', db_user.id), True, 6)
        except Exception:
            pass

        texts = get_texts(db_user.language)
        # Ограничим длину подтверждения чтобы не упереться в лимиты
        safe_title = html.escape(title if len(title) <= 200 else (title[:197] + '...'))
        creation_text = (
            f'✅ <b>Тикет #{ticket.id} создан</b>\n\n'
            f'📝 Заголовок: {safe_title}\n'
            f'📊 Статус: {ticket.status_emoji} '
            f'{texts.t("TICKET_STATUS_OPEN", "Открыт")}\n'
            f'📅 Создан: {format_local_datetime(ticket.created_at, "%d.%m.%Y %H:%M")}\n'
            + (f'📎 Вложение: {"видео" if media_type == "video" else "фото"}\n' if media_file_id else '')
        )

        data_prompt = await state.get_data()
        prompt_chat_id = data_prompt.get('prompt_chat_id')
        prompt_message_id = data_prompt.get('prompt_message_id')
        keyboard = types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('VIEW_TICKET', '👁️ Посмотреть тикет'), callback_data=f'view_ticket_{ticket.id}'
                    )
                ],
                [
                    types.InlineKeyboardButton(
                        text=texts.t('BACK_TO_MENU', '🏠 В главное меню'), callback_data='back_to_menu'
                    )
                ],
            ]
        )
        if prompt_chat_id and prompt_message_id:
            try:
                await bot.edit_message_text(
                    chat_id=prompt_chat_id,
                    message_id=prompt_message_id,
                    text=creation_text,
                    reply_markup=keyboard,
                    parse_mode='HTML',
                )
            except TelegramBadRequest:
                await answer(creation_text, reply_markup=keyboard, parse_mode='HTML')
        else:
            await answer(creation_text, reply_markup=keyboard, parse_mode='HTML')

        await state.clear()

        # Уведомить админов
        await notify_admins_about_new_ticket(ticket, db)
        schedule_assist(bot, ticket.id, 'new_ticket')

    except Exception as e:
        logger.error('Error creating ticket', error=e)
        texts = get_texts(db_user.language)
        await answer(texts.t('TICKET_CREATE_ERROR', '❌ Произошла ошибка при создании тикета. Попробуйте позже.'))


async def show_my_tickets(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)

    # Определяем текущую страницу
    current_page = 1
    if callback.data.startswith('my_tickets_page_'):
        try:
            current_page = int(callback.data.replace('my_tickets_page_', ''))
        except ValueError:
            current_page = 1

    # Пагинация открытых тикетов из БД
    per_page = 10
    total_open = await TicketCRUD.count_user_tickets_by_statuses(
        db, db_user.id, [TicketStatus.OPEN.value, TicketStatus.ANSWERED.value, TicketStatus.PENDING.value]
    )
    total_pages = max(1, (total_open + per_page - 1) // per_page)
    current_page = max(1, min(current_page, total_pages))
    offset = (current_page - 1) * per_page
    open_tickets = await TicketCRUD.get_user_tickets_by_statuses(
        db,
        db_user.id,
        [TicketStatus.OPEN.value, TicketStatus.ANSWERED.value, TicketStatus.PENDING.value],
        limit=per_page,
        offset=offset,
    )

    # Проверка на отсутствие тикетов совсем (ни открытых, ни закрытых)
    has_closed_any = await TicketCRUD.count_user_tickets_by_statuses(db, db_user.id, [TicketStatus.CLOSED.value]) > 0
    if not open_tickets and not has_closed_any:
        await callback.message.edit_text(
            texts.t('NO_TICKETS', 'У вас пока нет тикетов.'),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('CREATE_TICKET_BUTTON', '🎫 Создать тикет'), callback_data='create_ticket'
                        )
                    ],
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('VIEW_CLOSED_TICKETS', '🟢 Закрытые тикеты'), callback_data='my_tickets_closed'
                        )
                    ],
                    [types.InlineKeyboardButton(text=texts.BACK, callback_data='menu_support')],
                ]
            ),
        )
        await callback.answer()
        return

    # Открытые с пагинацией (DB)
    open_data = [{'id': t.id, 'title': t.title, 'status_emoji': t.status_emoji} for t in open_tickets]
    keyboard = get_my_tickets_keyboard(
        open_data,
        current_page=current_page,
        total_pages=total_pages,
        language=db_user.language,
        page_prefix='my_tickets_page_',
    )
    # Добавим кнопку перехода к закрытым
    keyboard.inline_keyboard.insert(
        0,
        [
            types.InlineKeyboardButton(
                text=texts.t('VIEW_CLOSED_TICKETS', '🟢 Закрытые тикеты'), callback_data='my_tickets_closed'
            )
        ],
    )
    # Всегда используем фото-рендер с логотипом (утилита сама сделает фоллбек при необходимости)
    await edit_or_answer_photo(
        callback=callback,
        caption=texts.t('MY_TICKETS_TITLE', '📋 Ваши тикеты:'),
        keyboard=keyboard,
        parse_mode='HTML',
    )
    await callback.answer()


async def show_my_tickets_closed(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    # Пагинация закрытых
    current_page = 1
    data_str = callback.data
    if data_str.startswith('my_tickets_closed_page_'):
        try:
            current_page = int(data_str.replace('my_tickets_closed_page_', ''))
        except ValueError:
            current_page = 1

    per_page = 10
    total_closed = await TicketCRUD.count_user_tickets_by_statuses(db, db_user.id, [TicketStatus.CLOSED.value])
    if total_closed == 0:
        await callback.message.edit_text(
            texts.t('NO_CLOSED_TICKETS', 'Закрытых тикетов пока нет.'),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('BACK_TO_OPEN_TICKETS', '🔴 Открытые тикеты'), callback_data='my_tickets'
                        )
                    ],
                    [types.InlineKeyboardButton(text=texts.BACK, callback_data='menu_support')],
                ]
            ),
        )
        await callback.answer()
        return
    total_pages = max(1, (total_closed + per_page - 1) // per_page)
    current_page = max(1, min(current_page, total_pages))
    offset = (current_page - 1) * per_page
    tickets = await TicketCRUD.get_user_tickets_by_statuses(
        db, db_user.id, [TicketStatus.CLOSED.value], limit=per_page, offset=offset
    )
    data = [{'id': t.id, 'title': t.title, 'status_emoji': t.status_emoji} for t in tickets]
    kb = get_my_tickets_keyboard(
        data,
        current_page=current_page,
        total_pages=total_pages,
        language=db_user.language,
        page_prefix='my_tickets_closed_page_',
    )
    kb.inline_keyboard.insert(
        0,
        [
            types.InlineKeyboardButton(
                text=texts.t('BACK_TO_OPEN_TICKETS', '🔴 Открытые тикеты'), callback_data='my_tickets'
            )
        ],
    )
    await edit_or_answer_photo(
        callback=callback,
        caption=texts.t('CLOSED_TICKETS_TITLE', '🟢 Закрытые тикеты:'),
        keyboard=kb,
        parse_mode='HTML',
    )
    await callback.answer()


async def view_ticket(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Показать детали тикета с пагинацией"""
    data_str = callback.data
    # None — страница не задана: открываем последнюю, где лежат свежие сообщения
    page = None
    ticket_id = None
    if data_str.startswith('ticket_view_page_'):
        # format: ticket_view_page_{ticket_id}_{page}
        try:
            _, _, _, tid, p = data_str.split('_')
            ticket_id = int(tid)
            page = max(1, int(p))
        except Exception:
            pass
    if ticket_id is None:
        ticket_id = int(data_str.replace('view_ticket_', ''))

    ticket = await TicketCRUD.get_ticket_by_id(db, ticket_id, load_messages=True)

    if not ticket or ticket.user_id != db_user.id:
        texts = get_texts(db_user.language)
        await callback.answer(texts.t('TICKET_NOT_FOUND', 'Тикет не найден.'), show_alert=True)
        return

    texts = get_texts(db_user.language)

    # Формируем текст тикета
    status_text = {
        TicketStatus.OPEN.value: texts.t('TICKET_STATUS_OPEN', 'Открыт'),
        TicketStatus.ANSWERED.value: texts.t('TICKET_STATUS_ANSWERED', 'Отвечен'),
        TicketStatus.CLOSED.value: texts.t('TICKET_STATUS_CLOSED', 'Закрыт'),
        TicketStatus.PENDING.value: texts.t('TICKET_STATUS_PENDING', 'В ожидании'),
    }.get(ticket.status, ticket.status)

    header = (
        f'🎫 Тикет #{ticket.id}\n\n'
        f'📝 Заголовок: {html.escape(ticket.title or "")}\n'
        f'📊 Статус: {ticket.status_emoji} {status_text}\n'
        f'📅 Создан: {format_local_datetime(ticket.created_at, "%d.%m.%Y %H:%M")}\n\n'
    )
    message_blocks: list[str] = []
    if ticket.messages:
        message_blocks.append(f'💬 Сообщения ({len(ticket.messages)}):\n\n')
        for msg in ticket.messages:
            sender = '👤 Вы' if msg.is_user_message else '🛠️ Поддержка'
            block = f'{sender} ({format_local_datetime(msg.created_at, "%d.%m %H:%M")}):\n{html.escape(msg.message_text or "")}\n\n'
            for item in get_ticket_media(msg):
                label = {'photo': 'фото', 'video': 'видео', 'document': 'файл'}[item['type']]
                block += f'📎 Вложение: {label}\n\n'
            message_blocks.append(block)
    pages = build_ticket_pages(header, message_blocks, max_len=TICKET_PAGE_MAX_LEN)
    total_pages = len(pages)
    page = total_pages if page is None else min(page, total_pages)

    keyboard = get_ticket_view_keyboard(
        ticket_id,
        ticket.is_closed,
        db_user.language,
    )
    # Если есть фото или видео — добавим кнопку для просмотра
    has_attachments = any(get_ticket_media(m) for m in ticket.messages or [])
    if has_attachments:
        try:
            keyboard.inline_keyboard.insert(
                0,
                [
                    types.InlineKeyboardButton(
                        text=texts.t('TICKET_ATTACHMENTS', '📎 Вложения'),
                        callback_data=f'ticket_attachments_{ticket_id}',
                    )
                ],
            )
        except Exception:
            pass
    # Пагинация
    if total_pages > 1:
        nav_row = []
        if page > 1:
            nav_row.append(
                types.InlineKeyboardButton(text='⬅️', callback_data=f'ticket_view_page_{ticket_id}_{page - 1}')
            )
        nav_row.append(types.InlineKeyboardButton(text=f'{page}/{total_pages}', callback_data='noop'))
        if page < total_pages:
            nav_row.append(
                types.InlineKeyboardButton(text='➡️', callback_data=f'ticket_view_page_{ticket_id}_{page + 1}')
            )
        if getattr(keyboard, 'inline_keyboard', None) is not None:
            keyboard.inline_keyboard.insert(0, nav_row)
    # Показываем как текст (чтобы не упереться в caption лимит)
    page_text = pages[page - 1]
    await safe_edit_or_resend(callback.message, page_text, keyboard)
    await callback.answer()


async def send_ticket_attachments(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    try:
        await callback.answer(texts.t('SENDING_ATTACHMENTS', '📎 Отправляю вложения...'))
    except Exception:
        pass
    try:
        ticket_id = int(callback.data.replace('ticket_attachments_', ''))
    except ValueError:
        await callback.answer(texts.t('TICKET_NOT_FOUND', 'Тикет не найден.'), show_alert=True)
        return

    ticket = await TicketCRUD.get_ticket_by_id(db, ticket_id, load_messages=True)
    if not ticket or ticket.user_id != db_user.id:
        await callback.answer(texts.t('TICKET_NOT_FOUND', 'Тикет не найден.'), show_alert=True)
        return

    attachments = [item for message in ticket.messages for item in get_ticket_media(message)]
    if not attachments:
        await callback.answer(texts.t('NO_ATTACHMENTS', 'Вложений нет.'), show_alert=True)
        return

    # Telegram ограничивает media group до 10 элементов. Отправим чанками.
    from aiogram.types import InputMediaDocument, InputMediaPhoto, InputMediaVideo

    chunks = ticket_media_chunks(attachments)
    last_group_message = None
    for chunk in chunks:
        media = [
            {'photo': InputMediaPhoto, 'video': InputMediaVideo, 'document': InputMediaDocument}[m['type']](
                media=m['file_id']
            )
            for m in chunk
        ]
        try:
            if len(media) == 1:
                item = media[0]
                sender = getattr(callback.message.bot, f'send_{item.type.value}')
                messages = [await sender(chat_id=callback.from_user.id, **{item.type: item.media})]
            else:
                messages = await callback.message.bot.send_media_group(chat_id=callback.from_user.id, media=media)
            if messages:
                last_group_message = messages[-1]
        except Exception:
            pass
    if last_group_message:
        try:
            kb = types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('DELETE_MESSAGE', '🗑 Удалить'),
                            callback_data=f'user_delete_message_{last_group_message.message_id}',
                        )
                    ]
                ]
            )
            await callback.message.bot.send_message(
                chat_id=callback.from_user.id, text=texts.t('ATTACHMENTS_SENT', 'Вложения отправлены.'), reply_markup=kb
            )
        except Exception:
            pass
    else:
        try:
            await callback.answer(texts.t('ATTACHMENTS_SENT', 'Вложения отправлены.'))
        except Exception:
            pass


async def user_delete_message(callback: types.CallbackQuery):
    try:
        msg_id = int(callback.data.replace('user_delete_message_', ''))
    except ValueError:
        await callback.answer('❌')
        return
    try:
        await callback.message.bot.delete_message(chat_id=callback.from_user.id, message_id=msg_id)
        await callback.message.delete()
    except Exception:
        pass
    await callback.answer('✅')


async def _edit_or_send(
    message: types.Message,
    chat_id: int | None,
    message_id: int | None,
    text: str,
    language: str,
    reply_markup: types.InlineKeyboardMarkup | None = None,
) -> None:
    """Попытаться отредактировать prompt-сообщение, при неудаче — отправить новое.

    Без ``reply_markup`` под сообщением кнопка отмены создания тикета.
    """
    markup = reply_markup or get_ticket_cancel_keyboard(language)
    if chat_id and message_id:
        try:
            await message.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                reply_markup=markup,
            )
            return
        except TelegramBadRequest:
            pass
    await message.answer(text, reply_markup=markup)


async def _try_delete_message_later(bot: Bot, chat_id: int, message_id: int, delay_seconds: float = 1.0):
    try:
        await asyncio.sleep(delay_seconds)
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        # В приватных чатах удаление сообщений пользователя может быть недоступно — игнорируем ошибки
        pass


async def reply_to_ticket(callback: types.CallbackQuery, state: FSMContext, db_user: User):
    """Начать ответ на тикет"""
    ticket_id = int(callback.data.replace('reply_ticket_', ''))

    await state.update_data(ticket_id=ticket_id)

    texts = get_texts(db_user.language)

    try:
        await callback.message.edit_text(
            texts.t('TICKET_REPLY_INPUT', 'Введите ваш ответ:'),
            reply_markup=get_ticket_reply_cancel_keyboard(db_user.language),
        )
    except TelegramBadRequest:
        try:
            await callback.message.delete()
        except Exception:
            pass
        await callback.message.answer(
            texts.t('TICKET_REPLY_INPUT', 'Введите ваш ответ:'),
            reply_markup=get_ticket_reply_cancel_keyboard(db_user.language),
        )

    await state.set_state(TicketStates.waiting_for_reply)
    await callback.answer()


async def handle_ticket_reply(message: types.Message, state: FSMContext, db_user: User, db: AsyncSession):
    # Проверяем, что пользователь в правильном состоянии
    current_state = await state.get_state()
    if current_state != TicketStates.waiting_for_reply:
        return

    # Защита от спама: по тикету принимаем только первое сообщение в коротком окне
    try:
        data_rl = await state.get_data()
        rl_ticket_id = data_rl.get('ticket_id') or 'reply'
        limited = await RateLimitCache.is_rate_limited(db_user.id, f'ticket_reply_{rl_ticket_id}', limit=1, window=2)
        if limited:
            try:
                asyncio.create_task(_try_delete_message_later(message.bot, message.chat.id, message.message_id, 2.0))
            except Exception as exc:
                logger.debug('Антиспам тикетов: сообщение не поставлено на удаление', error=str(exc))
            return
    except Exception:
        pass
    try:
        data_rl = await state.get_data()
        last_ts = data_rl.get('rl_ts_reply')
        now_ts = time.time()
        if last_ts and (now_ts - float(last_ts)) < 2:
            try:
                asyncio.create_task(_try_delete_message_later(message.bot, message.chat.id, message.message_id, 2.0))
            except Exception as exc:
                logger.debug('Антиспам тикетов: сообщение не поставлено на удаление', error=str(exc))
            return
        await state.update_data(rl_ts_reply=now_ts)
    except Exception as exc:
        # Антиспам — вспомогательный механизм: без состояния ответ всё равно обрабатывается.
        logger.debug('Антиспам ответа на тикет: состояние не обновлено', error=str(exc))

    """Обработать ответ на тикет"""
    # Фото и видео для ответа пользователя.
    reply_text = (message.text or message.caption or '').strip()
    media_type = None
    media_file_id = None
    media_caption = None
    if message.photo:
        media_type = 'photo'
        media_file_id = message.photo[-1].file_id
        media_caption = message.caption
    elif message.video:
        media_type = 'video'
        media_file_id = message.video.file_id
        media_caption = message.caption

    if len(reply_text) < 5 and not media_file_id:
        texts = get_texts(db_user.language)
        await message.answer(
            texts.t('TICKET_REPLY_TOO_SHORT', 'Ответ должен содержать минимум 5 символов. Попробуйте еще раз:')
        )
        return

    # Раньше ответ молча резался до 400 символов, и поддержка видела огрызок.
    if len(reply_text) > TICKET_MESSAGE_MAX_LENGTH:
        texts = get_texts(db_user.language)
        await message.answer(
            texts.t(
                'TICKET_REPLY_TOO_LONG',
                'Ответ слишком длинный. Максимум {limit} символов. Сократите текст и отправьте еще раз:',
            ).format(limit=TICKET_MESSAGE_MAX_LENGTH)
        )
        return

    data = await state.get_data()
    ticket_id = data.get('ticket_id')

    if not ticket_id:
        texts = get_texts(db_user.language)
        await message.answer(texts.t('TICKET_REPLY_ERROR', 'Ошибка: не найден ID тикета.'))
        await state.clear()
        return

    try:
        # Проверяем, что тикет принадлежит пользователю и не закрыт
        ticket = await TicketCRUD.get_ticket_by_id(db, ticket_id, load_messages=False)
        if not ticket or ticket.user_id != db_user.id:
            texts = get_texts(db_user.language)
            await message.answer(texts.t('TICKET_NOT_FOUND', 'Тикет не найден.'))
            await state.clear()
            return
        if ticket.status == TicketStatus.CLOSED.value:
            texts = get_texts(db_user.language)
            await message.answer(
                texts.t('TICKET_CLOSED', '✅ Тикет закрыт.'),
                reply_markup=types.InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            types.InlineKeyboardButton(
                                text=texts.t('CLOSE_NOTIFICATION', '❌ Закрыть уведомление'),
                                callback_data=f'close_ticket_notification_{ticket.id}',
                            )
                        ]
                    ]
                ),
            )
            await state.clear()
            return

        # Блокируем добавление сообщения, если тикет закрыт или заблокирован админом
        if ticket.status == TicketStatus.CLOSED.value or ticket.is_user_reply_blocked:
            texts = get_texts(db_user.language)
            await message.answer(
                texts.t('TICKET_CLOSED_NO_REPLY', '❌ Тикет закрыт, ответить невозможно.'),
                reply_markup=types.InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            types.InlineKeyboardButton(
                                text=texts.t('CLOSE_NOTIFICATION', '❌ Закрыть уведомление'),
                                callback_data=f'close_ticket_notification_{ticket.id}',
                            )
                        ]
                    ]
                ),
            )
            await state.clear()
            return

        # Добавляем сообщение в тикет
        await TicketMessageCRUD.add_message(
            db,
            ticket_id,
            db_user.id,
            reply_text,
            is_from_admin=False,
            media_type=media_type,
            media_file_id=media_file_id,
            media_caption=media_caption,
        )

        texts = get_texts(db_user.language)

        await message.answer(
            texts.t('TICKET_REPLY_SENT', '✅ Ваш ответ отправлен!'),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('VIEW_TICKET', '👁️ Посмотреть тикет'), callback_data=f'view_ticket_{ticket_id}'
                        )
                    ],
                    [
                        types.InlineKeyboardButton(
                            text=texts.t('BACK_TO_MENU', '🏠 В главное меню'), callback_data='back_to_menu'
                        )
                    ],
                ]
            ),
        )

        await state.clear()

        # Уведомить админов об ответе пользователя
        logger.info('Attempting to notify admins about ticket reply', ticket_id=ticket_id)
        await notify_admins_about_ticket_reply(
            ticket, reply_text, db, media_file_id=media_file_id, media_type=media_type
        )
        schedule_assist(message.bot, ticket_id, 'new_message')

    except Exception as e:
        logger.error('Error adding ticket reply', error=e)
        texts = get_texts(db_user.language)
        await message.answer(
            texts.t('TICKET_REPLY_ERROR', '❌ Произошла ошибка при отправке ответа. Попробуйте позже.')
        )


async def close_ticket(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Закрыть тикет"""
    ticket_id = int(callback.data.replace('close_ticket_', ''))

    try:
        # Проверяем, что тикет принадлежит пользователю
        ticket = await TicketCRUD.get_ticket_by_id(db, ticket_id, load_messages=False)
        if not ticket or ticket.user_id != db_user.id:
            texts = get_texts(db_user.language)
            await callback.answer(texts.t('TICKET_NOT_FOUND', 'Тикет не найден.'), show_alert=True)
            return

        # Запрещаем закрытие, если заблокирован для ответа? (не требуется) Закрываем тикет
        success = await TicketCRUD.close_ticket(db, ticket_id)

        if success:
            texts = get_texts(db_user.language)
            await callback.answer(texts.t('TICKET_CLOSED', '✅ Тикет закрыт.'), show_alert=True)

            # Обновляем inline-клавиатуру текущего сообщения (убираем кнопки)
            await callback.message.edit_reply_markup(
                reply_markup=get_ticket_view_keyboard(ticket_id, True, db_user.language)
            )
        else:
            texts = get_texts(db_user.language)
            await callback.answer(texts.t('TICKET_CLOSE_ERROR', '❌ Ошибка при закрытии тикета.'), show_alert=True)

    except Exception as e:
        logger.error('Error closing ticket', error=e)
        texts = get_texts(db_user.language)
        await callback.answer(texts.t('TICKET_CLOSE_ERROR', '❌ Ошибка при закрытии тикета.'), show_alert=True)


async def cancel_ticket_creation(callback: types.CallbackQuery, state: FSMContext, db_user: User):
    """Отменить создание тикета"""
    await state.clear()

    texts = get_texts(db_user.language)

    await callback.message.edit_text(
        texts.t('TICKET_CREATION_CANCELLED', 'Создание тикета отменено.'),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('BACK_TO_SUPPORT', '⬅️ К поддержке'), callback_data='menu_support'
                    )
                ]
            ]
        ),
    )
    await callback.answer()


async def cancel_ticket_reply(callback: types.CallbackQuery, state: FSMContext, db_user: User):
    """Отменить ответ на тикет"""
    await state.clear()

    texts = get_texts(db_user.language)

    await callback.message.edit_text(
        texts.t('TICKET_REPLY_CANCELLED', 'Ответ отменен.'),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [types.InlineKeyboardButton(text=texts.t('BACK_TO_TICKETS', '⬅️ К тикетам'), callback_data='my_tickets')]
            ]
        ),
    )
    await callback.answer()


async def close_ticket_notification(callback: types.CallbackQuery, db_user: User):
    """Закрыть уведомление о тикете"""
    texts = get_texts(db_user.language)

    # Проверяем, доступно ли сообщение для удаления
    if isinstance(callback.message, InaccessibleMessage):
        await callback.answer()
        return

    try:
        await callback.message.delete()
    except TelegramBadRequest:
        # Message is too old to delete (>48h) — edit it instead
        try:
            await callback.message.edit_text(texts.t('NOTIFICATION_CLOSED', 'Уведомление закрыто.'))
        except TelegramBadRequest:
            pass
    await callback.answer(texts.t('NOTIFICATION_CLOSED', 'Уведомление закрыто.'))


def build_ticket_card_keyboard(ticket: Ticket, user: User | None, *, role: str):
    """Клавиатура карточки тикета по роли получателя ('admin' / 'moderator' / 'group').

    Для группового/супергруппа админ-чата ('group') — урезанный набор без
    FSM-кнопок: «Ответить»/«Блок по времени» в общем чате не работают из-за
    privacy mode бота, остаются надёжные URL/callback-кнопки. Той же клавиатурой
    карточка перерисовывается после действий в группе (закрыть, блок, разблок) —
    иначе туда попадал экран личной админки с мёртвыми кнопками и «⬅️ Назад».
    """
    from app.config import settings

    # В cabinet-режиме добавляем кнопку «открыть тикет в кабинете»: web_app в личке,
    # t.me Mini App диплинк в группе (где web_app недоступен). None — если не
    # cabinet-режим / кабинет не настроен / в группе нет зарегистрированного Mini App.
    cabinet_button = build_admin_ticket_cabinet_button(
        ticket.id,
        text=get_texts(settings.DEFAULT_LANGUAGE).t('OPEN_TICKET_IN_CABINET', '🗂 Открыть в кабинете'),
        in_group=(role == 'group'),
    )
    return get_ticket_notification_keyboard(
        ticket.id,
        user_id=user.id if user else None,
        telegram_id=user.telegram_id if user else None,
        username=user.username if user else None,
        is_closed=ticket.is_closed,
        is_user_blocked=getattr(ticket, 'is_user_reply_blocked', False),
        is_admin=(role == 'admin'),
        fsm_enabled=(role != 'group'),
        cabinet_button=cabinet_button,
        language=settings.DEFAULT_LANGUAGE,
    )


def _build_ticket_notification_keyboard(service: AdminNotificationService, ticket: Ticket, user: User | None):
    """Клавиатура уведомления о тикете; None только для роли 'none'
    (посторонний в личке / строковый chat_id)."""
    role = service.resolve_recipient_role()
    if role == 'none':
        return None
    return build_ticket_card_keyboard(ticket, user, role=role)


async def notify_admins_about_new_ticket(ticket: Ticket, db: AsyncSession):
    """Уведомить админов о новом тикете"""
    try:
        from app.config import settings

        if not settings.is_admin_notifications_enabled():
            logger.info(
                'Admin notifications disabled. Ticket created by user', ticket_id=ticket.id, user_id=ticket.user_id
            )
            return

        get_texts(settings.DEFAULT_LANGUAGE)
        title = (ticket.title or '').strip()
        if len(title) > 60:
            title = title[:57] + '...'

        try:
            user = await get_user_by_id(db, ticket.user_id)
        except Exception:
            user = None
        full_name = html.escape(user.full_name or '') if user else 'Unknown'
        telegram_id_display = (user.telegram_id or user.email or f'#{user.id}') if user else '—'
        username_display = format_username_link(user.username if user else None, 'отсутствует')

        # Загружаем первое сообщение для получения медиа и превью текста
        first_message = await TicketMessageCRUD.get_first_message(db, ticket.id)
        media_file_id = None
        media_type = None
        message_preview = ''
        if first_message:
            media_file_id = first_message.media_file_id if first_message.has_media else None
            media_type = first_message.media_type if first_message.has_media else None
            msg_text = (first_message.message_text or '').strip()
            if msg_text:
                message_preview = preview_text(msg_text)

        safe_title = html.escape(title) if title else '—'

        notification_text = (
            f'🎫 <b>НОВЫЙ ТИКЕТ</b>\n\n'
            f'🆔 <b>ID:</b> <code>{ticket.id}</code>\n'
            f'👤 <b>Пользователь:</b> {full_name}\n'
            f'🆔 <b>ID:</b> <code>{telegram_id_display}</code>\n'
            f'📱 <b>Username:</b> {username_display}\n'
            f'📝 <b>Заголовок:</b> {safe_title}\n'
        )

        if message_preview:
            notification_text += f'\n📩 <b>Сообщение:</b>\n{html.escape(message_preview)}\n'

        # Ответы обязательной анкеты (текст «Другое» пишет клиент: экранируем).
        raw_diagnostics = getattr(ticket, 'diagnostics', None)
        diagnostics_text = (
            render_diagnostics(raw_diagnostics, 'ru', await get_form_options()) if raw_diagnostics else ''
        )
        if diagnostics_text:
            form_title = get_texts('ru').t('TICKET_FORM_TITLE')
            notification_text += f'\n📋 <b>{html.escape(form_title)}:</b>\n{html.escape(diagnostics_text)}\n'

        notification_text += f'\n📅 <b>Создан:</b> {format_local_datetime(ticket.created_at, "%d.%m.%Y %H:%M")}\n'

        from app.services.maintenance_service import maintenance_service

        bot = maintenance_service._bot or None
        if bot is None:
            logger.warning('Bot instance is not available for admin notifications')
            return

        service = AdminNotificationService(bot)

        # Определяем роль получателя до отправки (без I/O — данные в памяти).
        # В личном чате chat_id == telegram_id, что позволяет проверить права заранее.
        keyboard = _build_ticket_notification_keyboard(service, ticket, user)

        await service.send_ticket_event_notification(
            notification_text, keyboard, media_file_id=media_file_id, media_type=media_type
        )
    except Exception as e:
        logger.error('Error notifying admins about new ticket', error=e)


async def notify_admins_about_ticket_reply(
    ticket: Ticket,
    reply_text: str,
    db: AsyncSession,
    *,
    media_file_id: str | None = None,
    media_type: str | None = None,
):
    """Уведомить админов об ответе пользователя на тикет"""
    logger.info('notify_admins_about_ticket_reply called for ticket', ticket_id=ticket.id)
    try:
        from app.config import settings

        if not settings.is_admin_notifications_enabled():
            logger.info('Admin notifications disabled. Reply to ticket', ticket_id=ticket.id)
            return

        title = (ticket.title or '').strip()
        if len(title) > 60:
            title = title[:57] + '...'

        try:
            user = await get_user_by_id(db, ticket.user_id)
        except Exception:
            user = None
        full_name = html.escape(user.full_name or '') if user else 'Unknown'
        telegram_id_display = (user.telegram_id or user.email or f'#{user.id}') if user else '—'
        username_display = format_username_link(user.username if user else None, 'отсутствует')

        reply_preview = preview_text(reply_text)
        safe_title = html.escape(title) if title else '—'

        notification_text = (
            f'💬 <b>ОТВЕТ НА ТИКЕТ</b>\n\n'
            f'🆔 <b>ID тикета:</b> <code>{ticket.id}</code>\n'
            f'📝 <b>Заголовок:</b> {safe_title}\n'
            f'👤 <b>Пользователь:</b> {full_name}\n'
            f'🆔 <b>ID:</b> <code>{telegram_id_display}</code>\n'
            f'📱 <b>Username:</b> {username_display}\n\n'
            f'📩 <b>Сообщение:</b>\n{html.escape(reply_preview)}\n'
        )

        from app.services.maintenance_service import maintenance_service

        bot = maintenance_service._bot or None
        if bot is None:
            logger.warning('Bot instance is not available for admin notifications')
            return

        service = AdminNotificationService(bot)

        keyboard = _build_ticket_notification_keyboard(service, ticket, user)

        result = await service.send_ticket_event_notification(
            notification_text, keyboard, media_file_id=media_file_id, media_type=media_type
        )
        logger.info('Ticket reply notification sent', ticket_id=ticket.id, result=result)
    except Exception as e:
        logger.error('Error notifying admins about ticket reply', error=e)


def register_handlers(dp: Dispatcher):
    """Регистрация обработчиков тикетов"""

    # Создание тикета (теперь без приоритета)
    dp.callback_query.register(show_ticket_priority_selection, F.data == 'create_ticket')

    # Обязательная анкета (SUPPORT_TICKET_FORM_REQUIRED): кнопки tf:<поле>:<вариант>
    dp.callback_query.register(handle_ticket_form_answer, F.data.startswith('tf:'))
    dp.message.register(handle_ticket_form_other_input, TicketStates.waiting_for_form_other)
    dp.message.register(handle_ticket_form_stray_message, TicketStates.waiting_for_form)

    dp.message.register(handle_ticket_title_input, TicketStates.waiting_for_title)

    dp.message.register(handle_ticket_message_input, TicketStates.waiting_for_message)

    # Просмотр тикетов
    dp.callback_query.register(show_my_tickets, F.data == 'my_tickets')
    dp.callback_query.register(show_my_tickets_closed, F.data == 'my_tickets_closed')
    dp.callback_query.register(show_my_tickets_closed, F.data.startswith('my_tickets_closed_page_'))

    dp.callback_query.register(view_ticket, F.data.startswith('view_ticket_') | F.data.startswith('ticket_view_page_'))

    # Вложения пользователя
    dp.callback_query.register(send_ticket_attachments, F.data.startswith('ticket_attachments_'))

    dp.callback_query.register(user_delete_message, F.data.startswith('user_delete_message_'))

    # Ответы на тикеты
    dp.callback_query.register(reply_to_ticket, F.data.startswith('reply_ticket_'))

    dp.message.register(handle_ticket_reply, TicketStates.waiting_for_reply)

    # Закрытие тикетов
    dp.callback_query.register(close_ticket, F.data.regexp(r'^close_ticket_\d+$'))

    # Отмена операций
    dp.callback_query.register(cancel_ticket_creation, F.data == 'cancel_ticket_creation')

    dp.callback_query.register(cancel_ticket_reply, F.data == 'cancel_ticket_reply')

    # Пагинация тикетов
    dp.callback_query.register(show_my_tickets, F.data.startswith('my_tickets_page_'))

    # Закрытие уведомлений
    dp.callback_query.register(close_ticket_notification, F.data.startswith('close_ticket_notification_'))
