"""Mixin для интеграции с Antarctic Wallet (AW): пользователь платит USD, баланс растёт в рублях.

Поток: платёж создаётся заранее (``create_antarctic_payment``), пользователь открывает наше
мини-приложение внутри AW, страница вызывает ``start_antarctic_session`` (привязка кошелька и
создание pay-интента), дальше статус приходит вебхуком ``intent.approved`` /
``intent.rejected`` или находится повторным запросом интента (``refresh_antarctic_payment``).
Обе дороги сходятся в ``_apply_antarctic_intent`` — единственном месте, где платёж становится
оплаченным.

Инварианты денежного пути:

* зачисление ровно один раз: строка платежа берётся ``FOR UPDATE``, внутри блокировки
  проверяются ``is_paid`` и существующая транзакция по ``external_id``; между пометкой
  «оплачено» и зачислением баланса нет промежуточного ``commit``;
* TTL платежа останавливает только *новые* сессии: ``approved`` от AW зачисляется и после
  ``expires_at``;
* сумма из AW сверяется с сохранённой, расхождение — ``amount_mismatch`` без зачисления;
* ``app_secret`` и ``idToken`` нигде не логируются и в ответы не попадают.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal, InvalidOperation, localcontext
from importlib import import_module
from typing import Any

import structlog
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import PaymentMethod, TransactionType
from app.external.antarctic import AntarcticAPIError, AntarcticTokenError, get_antarctic_client
from app.utils.currency_converter import currency_converter
from app.utils.payment_logger import payment_logger as logger
from app.utils.user_utils import format_referrer_info


# Платёж, который не сходится по сумме или с чужой операцией, требует человека — сообщаем
# отдельным логгером вне фильтров app.payments (как у Cashera).
alert_logger = structlog.get_logger('app.antarctic_alert')

# Статус интента AW -> (внутренний статус, оплачен ли)
ANTARCTIC_STATUS_MAP: dict[str, tuple[str, bool]] = {
    'pending': ('pending', False),
    'approved': ('success', True),
    'rejected': ('rejected', False),
    'failed': ('failed', False),
    'expired': ('expired', False),
}

# Финальные неуспехи: поздний approved такой платёж уже не «чинит».
ANTARCTIC_TERMINAL_FAILURES = frozenset({'rejected', 'failed', 'expired', 'amount_mismatch'})

_WEBHOOK_EVENT_STATUS = {'intent.approved': 'approved', 'intent.rejected': 'rejected'}

# Длины колонок antarctic_payments, которые наполняются данными AW.
_MAX_LOGIN_LENGTH = 64
_MAX_OPERATION_ID_LENGTH = 64
_MAX_STATUS_LENGTH = 32
_MAX_FAILURE_REASON_LENGTH = 500
_MAX_TOKEN_LENGTH = 64

_SESSION_HTTP_STATUS = {
    'invalid_token': 401,
    'not_found': 404,
    'expired': 410,
    'login_mismatch': 409,
    'provider_error': 502,
}


class AntarcticSessionError(Exception):
    """Сессия мини-приложения не состоялась; ``code`` и ``http_status`` отдаются странице как есть."""

    def __init__(self, code: str) -> None:
        if code not in _SESSION_HTTP_STATUS:
            raise ValueError(f'unknown Antarctic session error code: {code}')
        super().__init__(code)
        self.code = code
        self.http_status = _SESSION_HTTP_STATUS[code]


def _to_decimal(value: Decimal | float | str) -> Decimal:
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except InvalidOperation:
        raise ValueError(f'not a number: {value!r}') from None


def usd_cents_for(amount_kopeks: int, usd_rub_rate: Decimal | float, markup_percent: Decimal | float) -> int:
    """Сумма к оплате в центах: ``ceil(₽ / курс × (1 + наценка/100) × 100)`` на ``Decimal``, без float-арифметики.

    Округление всегда вверх: пользователь не доплачивает меньше, чем стоит пополнение.
    """
    if isinstance(amount_kopeks, bool) or not isinstance(amount_kopeks, int) or amount_kopeks <= 0:
        raise ValueError('amount_kopeks must be a positive integer')
    rate = _to_decimal(usd_rub_rate)
    markup = _to_decimal(markup_percent)
    if not rate.is_finite() or rate <= 0:
        raise ValueError('usd_rub_rate must be a positive finite number')
    if not markup.is_finite():
        raise ValueError('markup_percent must be finite')

    # cents = kopeks / 100 / rate * (1 + markup / 100) * 100 = kopeks * (100 + markup) / (rate * 100)
    with localcontext() as context:
        context.prec = 60
        exact = Decimal(amount_kopeks) * (Decimal(100) + markup) / (rate * Decimal(100))
        cents = int(exact.to_integral_value(rounding=ROUND_CEILING))
    if cents < 1:
        raise ValueError('amount rounds to less than one cent')
    return cents


def _clean_str(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    return text or None


def _amounts_match(expected_usd: str, received: Any) -> bool:
    """Точное числовое равенство сумм: ``1.05`` == ``1.050``; всё нечисловое и неконечное — расхождение."""
    if isinstance(received, bool):
        return False
    try:
        value = Decimal(str(received).strip())
    except InvalidOperation:
        return False
    return value.is_finite() and value == Decimal(expected_usd)


def _failure_reason(raw: Any) -> str | None:
    if not isinstance(raw, dict):
        return None
    for key in ('failureReason', 'failure_reason'):
        reason = _clean_str(raw.get(key))
        if reason:
            return reason[:_MAX_FAILURE_REASON_LENGTH]
    return None


def _own_intent_amount(payment: Any, intent: Any) -> str:
    """Сумма интента для сверки в наших собственных запросах (сессия и сверка через API).

    Оба раза мы сами шлём в AW upsert с тем же ``reference_id``, ``type`` и ``data`` из строки
    платежа. Если AW не вернул ``data.amount``, сверяем с суммой нашего запроса: это не чужой
    ввод, а эхо того, что мы отправили. Вебхук так не делается — его тело пришло снаружи.
    """
    if intent.amount is not None:
        return intent.amount
    logger.info('Antarctic: AW не вернул сумму интента, сверяем с суммой нашего запроса', payment_id=payment.id)
    return payment.amount_usd


# Ключ в ``metadata_json``: платёж упёрся в чужой operation_id, сверка через API его больше не трогает.
_OPERATION_MISMATCH_KEY = 'operation_id_mismatch'


def _operation_mismatch_flagged(payment: Any) -> bool:
    metadata = getattr(payment, 'metadata_json', None)
    return isinstance(metadata, dict) and _OPERATION_MISMATCH_KEY in metadata


def _payment_state(payment: Any) -> dict[str, Any]:
    return {'payment': payment, 'status': payment.status or 'pending', 'is_paid': bool(payment.is_paid)}


class AntarcticPaymentMixin:
    """Mixin для работы с платежами Antarctic Wallet."""

    # ------------------------------------------------------------------ создание

    async def create_antarctic_payment(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        amount_kopeks: int,
        description: str = 'Пополнение баланса',
        language: str = 'ru',
    ) -> dict[str, Any] | None:
        """Создаёт платёж Antarctic Wallet (pending) и ссылки для пользователя.

        ``None`` — способ выключен, сумма вне лимитов, пользователя нет или курс недоступен.
        Сумма в USD фиксируется сейчас (курс сохраняется в строке) и не пересчитывается.
        """
        if not settings.is_antarctic_enabled():
            logger.error('Antarctic Wallet не настроен')
            return None

        if amount_kopeks < settings.ANTARCTIC_MIN_AMOUNT_KOPEKS or amount_kopeks > settings.ANTARCTIC_MAX_AMOUNT_KOPEKS:
            logger.warning(
                'Antarctic: сумма вне допустимого диапазона',
                amount_kopeks=amount_kopeks,
                min_kopeks=settings.ANTARCTIC_MIN_AMOUNT_KOPEKS,
                max_kopeks=settings.ANTARCTIC_MAX_AMOUNT_KOPEKS,
            )
            return None

        try:
            raw_rate = await currency_converter.get_usd_to_rub_rate()
            # Курс хранится с точностью колонки (4 знака), в центы идёт ровно то, что записано.
            rate = _to_decimal(raw_rate).quantize(Decimal('0.0001'), rounding=ROUND_HALF_UP)
            usd_cents = usd_cents_for(amount_kopeks, rate, settings.ANTARCTIC_MARKUP_PERCENT)
        except Exception as error:
            logger.error('Antarctic: курс USD/RUB недоступен, платёж не создан', error=error)
            return None

        payment_module = import_module('app.services.payment_service')
        user = await payment_module.get_user_by_id(db, user_id)
        if not user:
            # Платёж без пользователя нельзя будет зачислить — деньги застряли бы в AW.
            logger.error('Antarctic: пользователь не найден, платёж не создан', user_id=user_id)
            return None

        token = secrets.token_urlsafe(24)
        expires_at = datetime.now(UTC) + timedelta(minutes=settings.ANTARCTIC_PAYMENT_TTL_MINUTES)
        metadata = {
            'user_id': user_id,
            'amount_kopeks': amount_kopeks,
            'amount_usd_cents': usd_cents,
            'description': description,
            'language': language,
            'type': 'balance_topup',
        }

        antarctic_crud = import_module('app.database.crud.antarctic')
        payment = await antarctic_crud.create_antarctic_payment(
            db,
            user_id=user_id,
            token=token,
            amount_kopeks=amount_kopeks,
            amount_usd_cents=usd_cents,
            usd_rub_rate=rate,
            description=description,
            expires_at=expires_at,
            metadata_json=metadata,
        )

        logger.info(
            'Antarctic: создан платеж',
            payment_id=payment.id,
            user_id=user_id,
            amount_kopeks=amount_kopeks,
            amount_usd=payment.amount_usd,
        )

        return {
            'local_payment_id': payment.id,
            'token': token,
            'amount_kopeks': amount_kopeks,
            'amount_usd': payment.amount_usd,
            'app_link': settings.get_antarctic_app_link(token),
            'pay_page_url': settings.get_antarctic_pay_page_url(token),
            'referral_url': settings.get_antarctic_referral_url(),
            'expires_at': expires_at.isoformat(),
        }

    # ------------------------------------------------------------------ сессия мини-приложения

    async def start_antarctic_session(self, db: AsyncSession, *, token: str, id_token: str) -> dict[str, Any]:
        """Привязывает кошелёк к платежу и создаёт pay-интент в AW (вызывается страницей мини-приложения).

        Строка платежа блокируется на всё время вызова (HTTP до 15 с, пользователь один), чтобы
        две вкладки или перешедшая из рук в руки ссылка не создали два интента.
        """
        client = get_antarctic_client()
        if client is None:
            raise AntarcticSessionError('provider_error')

        try:
            login = await client.verify_id_token(id_token)
        except AntarcticTokenError:
            raise AntarcticSessionError('invalid_token') from None
        if not isinstance(login, str) or not login or len(login) > _MAX_LOGIN_LENGTH:
            logger.error('Antarctic: идентификатор кошелька из idToken не подходит для хранения')
            raise AntarcticSessionError('provider_error')

        if not isinstance(token, str) or not token or len(token) > _MAX_TOKEN_LENGTH:
            raise AntarcticSessionError('not_found')

        antarctic_crud = import_module('app.database.crud.antarctic')
        found = await antarctic_crud.get_antarctic_payment_by_token(db, token)
        if found is None:
            await db.rollback()
            raise AntarcticSessionError('not_found')
        payment_id = found.id

        payment = await antarctic_crud.get_antarctic_payment_by_id_for_update(db, payment_id)
        if payment is None:
            await db.rollback()
            raise AntarcticSessionError('not_found')

        try:
            return await self._start_antarctic_session_locked(db, client, payment, login)
        except AntarcticSessionError:
            raise
        except Exception:
            # Что бы ни пошло не так — блокировка строки не должна пережить запрос.
            await db.rollback()
            raise

    async def _start_antarctic_session_locked(
        self,
        db: AsyncSession,
        client: Any,
        payment: Any,
        login: str,
    ) -> dict[str, Any]:
        """Тело сессии при взятом FOR UPDATE; любой выход фиксирует или откатывает транзакцию."""
        antarctic_crud = import_module('app.database.crud.antarctic')
        payment_id = payment.id

        if payment.is_paid:
            state = self._antarctic_session_state(payment)
            await db.rollback()
            return state

        if payment.status in ANTARCTIC_TERMINAL_FAILURES:
            await db.rollback()
            raise AntarcticSessionError('expired')

        if payment.user_app_login and payment.user_app_login != login:
            await db.rollback()
            logger.warning('Antarctic: ссылка открыта из другого кошелька', payment_id=payment_id)
            raise AntarcticSessionError('login_mismatch')

        now = datetime.now(UTC)
        if not payment.operation_id and payment.expires_at is not None and now > payment.expires_at:
            payment.status = 'expired'
            payment.updated_at = now
            await db.commit()
            logger.info('Antarctic: срок платежа вышел до открытия страницы', payment_id=payment_id)
            raise AntarcticSessionError('expired')

        try:
            intent = await client.upsert_pay_intent(
                user_app_login=login,
                reference_id=payment.token,
                amount_usd=payment.amount_usd,
            )
        except AntarcticAPIError as error:
            await db.rollback()
            logger.warning(
                'Antarctic: AW не создал интент',
                payment_id=payment_id,
                status_code=error.status_code,
                code=error.code,
            )
            raise AntarcticSessionError('provider_error') from None

        stored_operation_id = payment.operation_id
        if stored_operation_id and stored_operation_id != intent.operation_id:
            # AW отвечает другой операцией на тот же reference_id. Какая из двух будет подтверждена,
            # неизвестно, а подтверждение чужой операции — это ещё одно списание у пользователя.
            # Ничего не переключаем и не отдаём странице.
            await db.rollback()
            alert_logger.error(
                'Antarctic: AW вернул другой operation_id для того же платежа — разобрать вручную',
                payment_id=payment_id,
                stored_operation_id=stored_operation_id,
                received_operation_id=intent.operation_id,
            )
            raise AntarcticSessionError('provider_error')
        if len(intent.operation_id) > _MAX_OPERATION_ID_LENGTH:
            await db.rollback()
            logger.error('Antarctic: operation_id слишком длинный для хранения', payment_id=payment_id)
            raise AntarcticSessionError('provider_error')

        payment.user_app_login = login
        payment.operation_id = intent.operation_id
        payment.aw_status = intent.status[:_MAX_STATUS_LENGTH]
        payment.updated_at = now
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            alert_logger.error(
                'Antarctic: operation_id уже занят другим платежом — разобрать вручную',
                payment_id=payment_id,
                operation_id=intent.operation_id,
            )
            raise AntarcticSessionError('provider_error') from None

        if intent.status in ANTARCTIC_STATUS_MAP and intent.status != 'pending':
            # Интент уже закрыт на стороне AW (например, оплачен до открытия страницы повторно).
            locked = await antarctic_crud.get_antarctic_payment_by_id_for_update(db, payment_id)
            if locked is not None:
                try:
                    await self._apply_antarctic_intent(
                        db,
                        locked,
                        status=intent.status,
                        operation_id=intent.operation_id,
                        amount=_own_intent_amount(locked, intent),
                        source='session',
                        raw=intent.raw,
                    )
                    await db.commit()
                except Exception:
                    await db.rollback()
                    raise

        await db.refresh(payment)
        return self._antarctic_session_state(payment)

    @staticmethod
    def _antarctic_session_state(payment: Any) -> dict[str, Any]:
        return {
            'operation_id': payment.operation_id,
            'amount_usd': payment.amount_usd,
            'amount_kopeks': payment.amount_kopeks,
            'status': payment.status or 'pending',
            'is_paid': bool(payment.is_paid),
        }

    # ------------------------------------------------------------------ сверка через API

    async def refresh_antarctic_payment(self, db: AsyncSession, local_payment_id: int) -> dict[str, Any] | None:
        """Сверяет платёж с AW повторным запросом интента и применяет статус.

        Запасной путь на случай потерянного вебхука: страница статуса, фоновая проверка и
        кнопка «Проверить статус». Работает и после ``expires_at`` — TTL не отменяет approved.
        """
        antarctic_crud = import_module('app.database.crud.antarctic')
        payment = await antarctic_crud.get_antarctic_payment_by_id(db, local_payment_id)
        if payment is None:
            logger.warning('Antarctic payment not found', payment_id=local_payment_id)
            return None

        if payment.is_paid or payment.status in ANTARCTIC_TERMINAL_FAILURES:
            return _payment_state(payment)

        payment_id = payment.id
        if _operation_mismatch_flagged(payment):
            # Расхождение operation_id уже подняло тревогу и ждёт человека: повторный запрос к AW
            # каждый цикл фоновой проверки дал бы тот же ответ и ту же тревогу.
            return _payment_state(payment)
        if not payment.user_app_login:
            # Страницу никто не открывал: интента в AW нет, оплатить нечем. Гасим по TTL.
            if payment.expires_at is None or datetime.now(UTC) <= payment.expires_at:
                return _payment_state(payment)
            return await self._expire_unopened_antarctic_payment(db, payment_id)

        client = get_antarctic_client()
        if client is None:
            logger.error('Antarctic: клиент AW недоступен, сверка пропущена', payment_id=payment_id)
            return _payment_state(payment)

        try:
            intent = await client.upsert_pay_intent(
                user_app_login=payment.user_app_login,
                reference_id=payment.token,
                amount_usd=payment.amount_usd,
            )
        except AntarcticAPIError as error:
            logger.warning(
                'Antarctic: не удалось получить статус через API',
                payment_id=payment_id,
                status_code=error.status_code,
                code=error.code,
            )
            return _payment_state(payment)
        except Exception as error:
            # Только имя класса: запрос к AW несёт Bearer-секрет, текст чужой ошибки в лог не нужен.
            logger.error('Antarctic: ошибка сверки через API', payment_id=payment_id, error=type(error).__name__)
            return _payment_state(payment)

        locked = await antarctic_crud.get_antarctic_payment_by_id_for_update(db, payment_id)
        if locked is None:
            logger.error('Antarctic: не удалось заблокировать платёж', payment_id=payment_id)
            return None

        try:
            await self._apply_antarctic_intent(
                db,
                locked,
                status=intent.status,
                operation_id=intent.operation_id,
                amount=_own_intent_amount(locked, intent),
                source='api_check',
                raw=intent.raw,
            )
            await db.commit()
        except Exception as error:
            # Сверка — запасной путь: сбой записи не должен ронять вызывающего, повторим в следующий раз.
            logger.exception('Antarctic: ошибка применения статуса при сверке', payment_id=payment_id, error=error)
            await db.rollback()

        await db.refresh(locked)
        return _payment_state(locked)

    async def _expire_unopened_antarctic_payment(self, db: AsyncSession, payment_id: int) -> dict[str, Any] | None:
        antarctic_crud = import_module('app.database.crud.antarctic')
        locked = await antarctic_crud.get_antarctic_payment_by_id_for_update(db, payment_id)
        if locked is None:
            return None

        # Всё решаем заново под блокировкой: за это время страницу могли открыть или вебхук мог прийти.
        still_unopened = (
            not locked.is_paid
            and locked.status not in ANTARCTIC_TERMINAL_FAILURES
            and not locked.user_app_login
            and not locked.operation_id
            and locked.expires_at is not None
            and datetime.now(UTC) > locked.expires_at
        )
        if still_unopened:
            locked.status = 'expired'
            locked.updated_at = datetime.now(UTC)
            await db.commit()
            logger.info('Antarctic: платеж истёк, страницу не открывали', payment_id=payment_id)
        else:
            await db.rollback()

        await db.refresh(locked)
        return _payment_state(locked)

    async def get_antarctic_payment_status(self, db: AsyncSession, local_payment_id: int) -> dict[str, Any] | None:
        """Статус по локальному id — для кнопки «Проверить статус» в боте."""
        return await self.refresh_antarctic_payment(db, local_payment_id)

    # ------------------------------------------------------------------ вебхук

    async def process_antarctic_webhook(self, db: AsyncSession, payload: dict[str, Any]) -> bool:
        """Обрабатывает вебхук AW (подпись уже проверена в webserver).

        True — принять (ответ 2xx). False — ответить 5xx, чтобы AW повторил доставку: только там,
        где повтор может помочь.
        """
        try:
            if not isinstance(payload, dict):
                logger.warning('Antarctic webhook: тело не объект, подтверждаем')
                return True

            event = payload.get('event')
            if event not in _WEBHOOK_EVENT_STATUS:
                logger.info('Antarctic webhook: событие не про платёж, подтверждаем', antarctic_event=event)
                return True

            data = payload.get('data')
            if not isinstance(data, dict) or data.get('type') != 'pay':
                logger.info('Antarctic webhook: не pay-интент, подтверждаем', antarctic_event=event)
                return True

            reference_id = _clean_str(data.get('reference_id') or data.get('referenceId'))
            operation_id = _clean_str(data.get('operation_id') or data.get('operationId'))

            antarctic_crud = import_module('app.database.crud.antarctic')
            payment = None
            if reference_id:
                payment = await antarctic_crud.get_antarctic_payment_by_token(db, reference_id)
            if payment is None and operation_id:
                payment = await antarctic_crud.get_antarctic_payment_by_operation_id(db, operation_id)
            if payment is None:
                # Не наш платёж (другая интеграция того же приложения) — повтор не поможет.
                logger.warning('Antarctic webhook: платеж не найден', operation_id=operation_id)
                return True

            status = _clean_str(data.get('status')) or _WEBHOOK_EVENT_STATUS[event]
            inner = data.get('data')
            amount = inner.get('amount') if isinstance(inner, dict) else None

            if status.lower() == 'approved' and _clean_str(amount) is None:
                # Зачислять по вебхуку без подтверждённой суммы нельзя — сверяем с AW своим запросом.
                return await self._settle_amountless_antarctic_webhook(db, payment, operation_id)

            locked = await antarctic_crud.get_antarctic_payment_by_id_for_update(db, payment.id)
            if locked is None:
                logger.error('Antarctic: не удалось заблокировать платёж', payment_id=payment.id)
                return False

            result = await self._apply_antarctic_intent(
                db,
                locked,
                status=status,
                operation_id=operation_id,
                amount=amount,
                source='webhook',
                raw=data,
            )
            # Отпускаем блокировку: всё нужное уже зафиксировано внутри, здесь коммитить нечего.
            await db.commit()
            return result
        except Exception as error:
            logger.exception('Antarctic webhook: ошибка обработки', error=error)
            try:
                await db.rollback()
            except Exception as rollback_error:
                logger.error('Antarctic webhook: не удалось откатить транзакцию', error=rollback_error)
            return False

    async def _settle_amountless_antarctic_webhook(
        self,
        db: AsyncSession,
        payment: Any,
        operation_id: str | None,
    ) -> bool:
        """``approved`` без ``data.amount``: вебхук сам ничего не зачисляет, итог берём из запроса к AW.

        Запрос к AW идёт без блокировки строки (``refresh_antarctic_payment`` берёт ``FOR UPDATE``
        только для записи результата). True — платёж закрыт (оплачен или финально неуспешен),
        False — AW повторит доставку: интент ещё не подтверждён или логин кошелька не сохранён.

        Любой ``approved``, после которого строка осталась неоплаченной и закрытой, — повод
        для человека: AW считает платёж оплаченным.
        """
        payment_id = payment.id
        stored_operation_id = payment.operation_id
        if stored_operation_id and operation_id and stored_operation_id != operation_id:
            alert_logger.error(
                'Antarctic: событие по другой операции, платёж не тронут — разобрать вручную',
                payment_id=payment_id,
                stored_operation_id=stored_operation_id,
                received_operation_id=operation_id,
                aw_status='approved',
                source='webhook',
            )
            return True

        if payment.is_paid:
            return True

        status_before = payment.status
        if status_before in ANTARCTIC_TERMINAL_FAILURES:
            # Сверка здесь ничего не изменит (платёж закрыт), а AW считает его оплаченным.
            self._alert_antarctic_approved_without_credit(payment_id, status_before)
            return True

        if not payment.user_app_login:
            # Без логина кошелька сверка через API невозможна, а ``refresh`` принял бы строку за
            # «страницу не открывали» и по TTL погасил бы её как expired — платёж, который AW
            # уже подтвердил. Строку не трогаем, пусть AW повторит доставку.
            alert_logger.error(
                'Antarctic: approved от AW по платежу без привязанного кошелька — сверка невозможна, разобрать вручную',
                payment_id=payment_id,
                operation_id=operation_id,
                current_status=status_before,
                source='webhook',
            )
            return False

        logger.warning('Antarctic webhook: approved без суммы, сверяем через API', payment_id=payment_id)
        refreshed = await self.refresh_antarctic_payment(db, payment_id)
        if refreshed is None:
            return False
        settled = bool(refreshed['is_paid']) or refreshed['status'] in ANTARCTIC_TERMINAL_FAILURES
        if not refreshed['is_paid'] and refreshed['status'] in ANTARCTIC_TERMINAL_FAILURES:
            # Расхождение суммы уже подняло тревогу при применении статуса; остальные причины закрытия — нет.
            if refreshed['status'] != 'amount_mismatch':
                self._alert_antarctic_approved_without_credit(payment_id, refreshed['status'])
        # Блокировку держать нечем, но чтение могло оставить транзакцию открытой.
        await db.commit()
        return settled

    @staticmethod
    def _alert_antarctic_approved_without_credit(payment_id: int, current_status: str) -> None:
        alert_logger.error(
            'Antarctic: approved по платежу в финальном неуспешном статусе — зачисление не сделано, разобрать вручную',
            payment_id=payment_id,
            current_status=current_status,
            source='webhook',
        )

    # ------------------------------------------------------------------ применение статуса

    async def _apply_antarctic_intent(
        self,
        db: AsyncSession,
        payment: Any,
        *,
        status: str,
        operation_id: str | None,
        amount: str | None,
        source: str,
        raw: dict[str, Any],
    ) -> bool:
        """Применяет состояние интента AW к платежу (FOR UPDATE уже взят вызывающим).

        Общая логика вебхука, страницы и сверки через API. True — событие принято (повтор не
        нужен), False — повтор имеет смысл (approved без суммы).
        """
        incoming = (_clean_str(status) or '').lower()
        incoming_operation_id = _clean_str(operation_id)

        if payment.is_paid:
            logger.info('Antarctic: платеж уже обработан', payment_id=payment.id, source=source, aw_status=incoming)
            return True

        if payment.status in ANTARCTIC_TERMINAL_FAILURES:
            if incoming == 'approved':
                # Платёж у нас закрыт как неуспешный, а AW говорит «оплачено»: деньги могли уйти.
                alert_logger.error(
                    'Antarctic: approved по платежу в финальном неуспешном статусе — зачисление не сделано, разобрать вручную',
                    payment_id=payment.id,
                    current_status=payment.status,
                    source=source,
                )
            else:
                logger.warning(
                    'Antarctic: платёж в финальном неуспешном статусе, событие игнорируется',
                    payment_id=payment.id,
                    current_status=payment.status,
                    aw_status=incoming,
                    source=source,
                )
            return True

        if payment.operation_id and incoming_operation_id and payment.operation_id != incoming_operation_id:
            alert_logger.error(
                'Antarctic: событие по другой операции, платёж не тронут — разобрать вручную',
                payment_id=payment.id,
                stored_operation_id=payment.operation_id,
                received_operation_id=incoming_operation_id,
                aw_status=incoming,
                source=source,
            )
            if source == 'api_check' and not _operation_mismatch_flagged(payment):
                # Фоновая сверка приходит каждый цикл с тем же ответом: помечаем строку, чтобы тревога
                # была одна. Вебхуки не помечаем — это может быть запоздавшая доставка старой операции.
                payment.metadata_json = {
                    **(payment.metadata_json if isinstance(payment.metadata_json, dict) else {}),
                    _OPERATION_MISMATCH_KEY: {
                        'received_operation_id': incoming_operation_id,
                        'detected_at': datetime.now(UTC).isoformat(),
                    },
                }
            return True

        if incoming not in ANTARCTIC_STATUS_MAP:
            # В том числе 'unknown': AW не прислал статус — ничего не меняем, сверка повторится.
            logger.warning('Antarctic: неизвестный статус', payment_id=payment.id, aw_status=incoming, source=source)
            return True

        internal_status, is_paid = ANTARCTIC_STATUS_MAP[incoming]
        now = datetime.now(UTC)
        amount_text = _clean_str(amount)
        callback_payload = {
            'source': source,
            'status': incoming,
            'operation_id': incoming_operation_id,
            'amount': amount_text,
            'received_at': now.isoformat(),
            'raw': raw if isinstance(raw, dict) else None,
        }

        # Подписанное событие могло прийти раньше, чем мы сохранили операцию (сбой после ответа AW):
        # запоминаем её вместе с остальными изменениями, но не при отказе «повторите позже».
        adopt_operation_id = (
            incoming_operation_id
            if incoming_operation_id
            and not payment.operation_id
            and len(incoming_operation_id) <= _MAX_OPERATION_ID_LENGTH
            else None
        )

        if is_paid:
            if amount_text is None:
                # Страховка для прямых вызовов: вебхук без суммы сюда не попадает (его сверяют через
                # API), а наши запросы подставляют сумму запроса. Без суммы не зачисляем.
                logger.error('Antarctic: approved без суммы, зачисление отменено', payment_id=payment.id, source=source)
                return False

            if not _amounts_match(payment.amount_usd, amount_text):
                alert_logger.error(
                    'Antarctic amount mismatch — платёж не зачислен, разобрать вручную',
                    payment_id=payment.id,
                    expected_usd=payment.amount_usd,
                    received_amount=amount_text[:32],
                    source=source,
                )
                if adopt_operation_id:
                    payment.operation_id = adopt_operation_id
                payment.status = 'amount_mismatch'
                payment.is_paid = False
                payment.aw_status = incoming[:_MAX_STATUS_LENGTH]
                payment.failure_reason = f'amount_mismatch: expected {payment.amount_usd}, received {amount_text[:32]}'
                payment.callback_payload = callback_payload
                payment.updated_at = now
                await db.commit()
                # Повтор не исправит расхождение — подтверждаем, платёж ждёт разбора.
                return True

            if adopt_operation_id:
                payment.operation_id = adopt_operation_id
            payment.status = internal_status
            payment.is_paid = True
            payment.aw_status = incoming[:_MAX_STATUS_LENGTH]
            payment.paid_at = now
            payment.callback_payload = callback_payload
            payment.updated_at = now
            # Без промежуточного commit — он снял бы FOR UPDATE lock до зачисления.
            await db.flush()
            return await self._finalize_antarctic_payment(db, payment, trigger=source)

        if (
            internal_status == payment.status
            and incoming[:_MAX_STATUS_LENGTH] == payment.aw_status
            and not adopt_operation_id
        ):
            # Фоновая сверка каждый цикл приносит тот же «pending» — без записи в БД.
            return True

        if adopt_operation_id:
            payment.operation_id = adopt_operation_id
        payment.status = internal_status
        payment.aw_status = incoming[:_MAX_STATUS_LENGTH]
        payment.callback_payload = callback_payload
        payment.updated_at = now
        reason = _failure_reason(raw)
        if reason:
            payment.failure_reason = reason
        await db.commit()
        return True

    async def _finalize_antarctic_payment(self, db: AsyncSession, payment: Any, *, trigger: str) -> bool:
        """Создаёт транзакцию, начисляет баланс и отправляет уведомления.

        FOR UPDATE lock уже взят вызывающим; платёж уже помечен ``is_paid`` (flush, без commit).
        Транзакция, привязка и баланс фиксируются одним commit — либо всё, либо ничего.
        """
        payment_module = import_module('app.services.payment_service')
        antarctic_crud = import_module('app.database.crud.antarctic')

        if payment.transaction_id:
            logger.info(
                'Antarctic платеж уже связан с транзакцией',
                payment_id=payment.id,
                transaction_id=payment.transaction_id,
                trigger=trigger,
            )
            await db.commit()
            return True

        metadata = dict(getattr(payment, 'metadata_json', {}) or {})

        user = await payment_module.get_user_by_id(db, payment.user_id)
        if not user:
            logger.error('Пользователь не найден для Antarctic', payment_id=payment.id, user_id=payment.user_id)
            # Откатываем и пометку «оплачено»: иначе любой следующий commit в этой сессии
            # зафиксировал бы оплаченный платёж без зачисления.
            await db.rollback()
            return False

        await db.refresh(user, attribute_names=['promo_group', 'user_promo_groups'])
        for user_promo_group in getattr(user, 'user_promo_groups', []):
            await db.refresh(user_promo_group, attribute_names=['promo_group'])

        promo_group = user.get_primary_promo_group()
        subscription = getattr(user, 'subscription', None)
        referrer_info = format_referrer_info(user)

        transaction_external_id = payment.token
        existing_transaction = await payment_module.get_transaction_by_external_id(
            db,
            transaction_external_id,
            PaymentMethod.ANTARCTIC,
        )
        if existing_transaction:
            # Депозит и баланс фиксируются одним commit, так что депозит по этому токену означает
            # «уже зачислено». Второй раз баланс не трогаем — только связываем платёж.
            logger.warning(
                'Antarctic: депозит по токену уже существует, повторное зачисление пропущено',
                payment_id=payment.id,
                transaction_id=existing_transaction.id,
                trigger=trigger,
            )
            await antarctic_crud.link_antarctic_payment_to_transaction(
                db, payment=payment, transaction_id=existing_transaction.id
            )
            await db.commit()
            return True

        display_name = settings.get_antarctic_display_name()
        description = f'Пополнение через {display_name} (${payment.amount_usd})'

        transaction = await payment_module.create_transaction(
            db,
            user_id=payment.user_id,
            type=TransactionType.DEPOSIT,
            amount_kopeks=payment.amount_kopeks,
            description=description,
            payment_method=PaymentMethod.ANTARCTIC,
            external_id=transaction_external_id,
            is_completed=True,
            created_at=getattr(payment, 'created_at', None),
            commit=False,
        )

        await antarctic_crud.link_antarctic_payment_to_transaction(db, payment=payment, transaction_id=transaction.id)

        from app.database.crud.user import lock_user_for_update

        user = await lock_user_for_update(db, user)

        old_balance = user.balance_kopeks
        was_first_topup = not user.has_made_first_topup

        user.balance_kopeks += payment.amount_kopeks
        user.updated_at = datetime.now(UTC)
        await db.commit()
        await db.refresh(user)

        from app.database.crud.transaction import emit_transaction_side_effects

        try:
            await emit_transaction_side_effects(
                db,
                transaction,
                amount_kopeks=payment.amount_kopeks,
                user_id=payment.user_id,
                type=TransactionType.DEPOSIT,
                payment_method=PaymentMethod.ANTARCTIC,
                external_id=transaction_external_id,
            )
        except Exception as error:
            # Баланс уже зачислен и зафиксирован: сбой побочных эффектов не должен превращаться в 5xx.
            logger.error('Ошибка побочных эффектов транзакции Antarctic', payment_id=payment.id, error=error)

        topup_status = '\U0001f195 Первое пополнение' if was_first_topup else '\U0001f504 Пополнение'

        try:
            from app.services.referral_service import process_referral_topup

            await process_referral_topup(db, user.id, payment.amount_kopeks, getattr(self, 'bot', None))
        except Exception as error:
            logger.error('Ошибка обработки реферального пополнения Antarctic', error=error)

        if was_first_topup and not user.has_made_first_topup and not user.referred_by_id:
            user.has_made_first_topup = True
            await db.commit()
            await db.refresh(user)

        if getattr(self, 'bot', None):
            try:
                from app.services.admin_notification_service import AdminNotificationService

                notification_service = AdminNotificationService(self.bot)
                await notification_service.send_balance_topup_notification(
                    user,
                    transaction,
                    old_balance,
                    topup_status=topup_status,
                    referrer_info=referrer_info,
                    subscription=subscription,
                    promo_group=promo_group,
                    db=db,
                )
            except Exception as error:
                logger.error('Ошибка отправки админ уведомления Antarctic', error=error)

        if getattr(self, 'bot', None) and user.telegram_id and settings.is_notifications_enabled():
            try:
                keyboard = await self.build_topup_success_keyboard(user)
                await self.bot.send_message(
                    user.telegram_id,
                    (
                        '✅ <b>Пополнение успешно!</b>\n\n'
                        f'\U0001f4b0 Сумма: {settings.format_price(payment.amount_kopeks)}\n'
                        f'\U0001f4b3 Способ: {settings.get_antarctic_display_name_html()}\n'
                        f'\U0001f194 Транзакция: {transaction.id}\n\n'
                        'Баланс пополнен автоматически!'
                    ),
                    parse_mode='HTML',
                    reply_markup=keyboard,
                )
            except Exception as error:
                logger.error('Ошибка отправки уведомления пользователю Antarctic', error=error)

        try:
            from app.services.payment.common import send_cart_notification_after_topup

            await send_cart_notification_after_topup(user, payment.amount_kopeks, db, getattr(self, 'bot', None))
        except Exception as error:
            logger.error(
                'Ошибка при работе с сохраненной корзиной для пользователя',
                user_id=payment.user_id,
                error=error,
                exc_info=True,
            )

        metadata['balance_change'] = {
            'old_balance': old_balance,
            'new_balance': user.balance_kopeks,
            'credited_at': datetime.now(UTC).isoformat(),
        }
        metadata['balance_credited'] = True
        payment.metadata_json = metadata
        await db.commit()

        logger.info('Обработан Antarctic платеж', payment_id=payment.id, user_id=payment.user_id, trigger=trigger)
        return True
