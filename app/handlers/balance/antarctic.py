"""Handlers for Antarctic Wallet balance top-ups (одна кнопка → сумма → ссылка на мини-приложение кошелька)."""

import html

import structlog
from aiogram import types
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import User
from app.keyboards.inline import get_back_keyboard
from app.keyboards.topup_amounts import get_topup_amount_keyboard
from app.localization.texts import get_texts
from app.services.payment_service import PaymentService
from app.states import BalanceStates
from app.utils.decorators import error_handler
from app.utils.formatters import format_minutes_declension
from app.utils.topup_retry import format_topup_amount_retry


logger = structlog.get_logger(__name__)


def _restriction_keyboard(texts) -> types.InlineKeyboardMarkup:
    keyboard = []
    support_url = settings.get_support_contact_url()
    if support_url:
        keyboard.append([types.InlineKeyboardButton(text='🆘 Обжаловать', url=support_url)])
    keyboard.append([types.InlineKeyboardButton(text=texts.BACK, callback_data='menu_balance')])
    return types.InlineKeyboardMarkup(inline_keyboard=keyboard)


def _restriction_text(db_user: User) -> str:
    reason = html.escape(getattr(db_user, 'restriction_reason', None) or 'Действие ограничено администратором')
    return f'🚫 <b>Пополнение ограничено</b>\n\n{reason}\n\nЕсли вы считаете это ошибкой, вы можете обжаловать решение.'


@error_handler
async def start_antarctic_payment(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    texts = get_texts(db_user.language)

    if getattr(db_user, 'restriction_topup', False):
        await callback.message.edit_text(_restriction_text(db_user), reply_markup=_restriction_keyboard(texts))
        await callback.answer()
        return

    if not settings.is_antarctic_enabled():
        await callback.answer(
            texts.t('ANTARCTIC_TEMPORARILY_UNAVAILABLE', '❌ Оплата через Antarctic Wallet временно недоступна'),
            show_alert=True,
        )
        return

    min_amount_label = settings.format_price(settings.ANTARCTIC_MIN_AMOUNT_KOPEKS)
    max_amount_label = settings.format_price(settings.ANTARCTIC_MAX_AMOUNT_KOPEKS)
    keyboard = await get_topup_amount_keyboard('antarctic', db_user.language, back_callback='back_to_menu')

    await callback.message.edit_text(
        texts.t(
            'ANTARCTIC_ENTER_AMOUNT',
            '🐧 <b>Оплата через {name}</b>\n\nВведите сумму пополнения от {min_amount} до {max_amount}. '
            'Списание в долларах по курсу ЦБ, комиссия 0%.',
        ).format(
            name=settings.get_antarctic_display_name_html(),
            min_amount=min_amount_label,
            max_amount=max_amount_label,
        ),
        reply_markup=keyboard,
        parse_mode='HTML',
    )

    await state.set_state(BalanceStates.waiting_for_amount)
    await state.update_data(
        payment_method='antarctic',
        antarctic_prompt_message_id=callback.message.message_id,
        antarctic_prompt_chat_id=callback.message.chat.id,
    )
    await callback.answer()


@error_handler
async def process_antarctic_payment_amount(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
    amount_kopeks: int,
    state: FSMContext,
):
    texts = get_texts(db_user.language)

    if getattr(db_user, 'restriction_topup', False):
        await message.answer(
            _restriction_text(db_user),
            reply_markup=_restriction_keyboard(texts),
            parse_mode='HTML',
        )
        await state.clear()
        return

    if not settings.is_antarctic_enabled():
        await message.answer(
            texts.t('ANTARCTIC_TEMPORARILY_UNAVAILABLE', '❌ Оплата через Antarctic Wallet временно недоступна')
        )
        return

    min_kopeks = settings.ANTARCTIC_MIN_AMOUNT_KOPEKS
    max_kopeks = settings.ANTARCTIC_MAX_AMOUNT_KOPEKS
    if amount_kopeks < min_kopeks or amount_kopeks > max_kopeks:
        await message.answer(
            format_topup_amount_retry(db_user.language, min_kopeks, max_kopeks, too_low=amount_kopeks < min_kopeks),
            reply_markup=get_back_keyboard(db_user.language, callback_data='balance_topup'),
        )
        await state.set_state(BalanceStates.waiting_for_amount)
        return

    try:
        payment_service = PaymentService(message.bot)
        result = await payment_service.create_antarctic_payment(
            db=db,
            user_id=db_user.id,
            amount_kopeks=amount_kopeks,
            description=settings.get_balance_payment_description(amount_kopeks, telegram_user_id=db_user.telegram_id),
            language=db_user.language,
        )
    except Exception as error:
        logger.exception('Ошибка создания платежа Antarctic Wallet', error=error)
        result = None

    if not result or not result.get('app_link'):
        await message.answer(
            texts.t(
                'ANTARCTIC_PAYMENT_ERROR',
                '❌ Не удалось создать платёж Antarctic Wallet. Попробуйте позже или обратитесь в поддержку.',
            )
        )
        await state.clear()
        return

    referral_url = result.get('referral_url')
    rows = [
        [
            types.InlineKeyboardButton(
                text=texts.t('ANTARCTIC_PAY_BUTTON', '💳 Оплатить в Antarctic Wallet'),
                url=result['app_link'],
            )
        ]
    ]
    if referral_url:
        rows.append(
            [
                types.InlineKeyboardButton(
                    text=texts.t('ANTARCTIC_REFERRAL_BUTTON', '🆕 Нет кошелька? Создать'),
                    url=referral_url,
                )
            ]
        )
    rows.append(
        [
            types.InlineKeyboardButton(
                text=texts.t('CHECK_STATUS_BUTTON', '📊 Проверить статус'),
                callback_data=f'check_antarctic_{result.get("local_payment_id")}',
            )
        ]
    )
    rows.append([types.InlineKeyboardButton(text=texts.BACK, callback_data='balance_topup')])

    text = texts.t(
        'ANTARCTIC_PAYMENT_CREATED',
        '🐧 <b>Оплата через {name}</b>\n\n'
        'К пополнению: <b>{amount}</b>\n'
        'К оплате: <b>${amount_usd}</b>\n\n'
        'Нажмите «Оплатить» — откроется Antarctic Wallet. Ссылка действует {ttl}, '
        'баланс пополнится автоматически.',
    ).format(
        name=settings.get_antarctic_display_name_html(),
        amount=settings.format_price(amount_kopeks),
        amount_usd=result.get('amount_usd'),
        ttl=format_minutes_declension(settings.ANTARCTIC_PAYMENT_TTL_MINUTES, db_user.language),
    )
    if referral_url:
        text += '\n\n' + texts.t('ANTARCTIC_REFERRAL_HINT', 'Нет кошелька? Создайте за минуту, пополнение по СБП.')

    data = await state.get_data()
    prompt_message_id = data.get('antarctic_prompt_message_id')
    prompt_chat_id = data.get('antarctic_prompt_chat_id', message.chat.id)
    try:
        await message.delete()
    except Exception as delete_error:  # pragma: no cover - зависит от прав бота
        logger.warning('Не удалось удалить сообщение с суммой Antarctic Wallet', delete_error=delete_error)
    if prompt_message_id:
        try:
            await message.bot.delete_message(prompt_chat_id, prompt_message_id)
        except Exception as delete_error:  # pragma: no cover - диагностический лог
            logger.warning('Не удалось удалить сообщение с запросом суммы Antarctic Wallet', delete_error=delete_error)

    await message.answer(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=rows), parse_mode='HTML')
    await state.clear()


@error_handler
async def check_antarctic_payment_status(callback: types.CallbackQuery, db: AsyncSession):
    try:
        local_payment_id = int(callback.data.rsplit('_', 1)[-1])
    except ValueError:
        await callback.answer('❌ Некорректный идентификатор платежа', show_alert=True)
        return

    try:
        status_info = await PaymentService(callback.bot).get_antarctic_payment_status(db, local_payment_id)
    except Exception as error:
        logger.exception('Ошибка проверки статуса Antarctic Wallet', error=error)
        await callback.answer('⚠️ Ошибка проверки статуса', show_alert=True)
        return

    if not status_info:
        await callback.answer('⚠️ Платёж не найден', show_alert=True)
        return

    payment = status_info.get('payment')
    user = getattr(payment, 'user', None)
    texts = get_texts(getattr(user, 'language', None) or 'ru')

    status = status_info.get('status') or 'pending'
    if status_info.get('is_paid'):
        label = texts.t('ANTARCTIC_STATUS_SUCCESS', '✅ Платёж зачислен')
    elif status == 'pending':
        label = texts.t('ANTARCTIC_STATUS_PENDING', '⏳ Ожидает оплаты')
    else:
        label = texts.t('ANTARCTIC_STATUS_FAILED', '❌ Платёж не прошёл или истёк')
    await callback.answer(label, show_alert=True)
