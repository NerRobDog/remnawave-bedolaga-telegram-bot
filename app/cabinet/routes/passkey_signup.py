"""Аккаунт одним passkey и recovery-коды.

Три роутера:
- ``signup_router`` (``/auth/passkey/signup``) — публичный: создать аккаунт ключом;
- ``recovery_auth_router`` (``/auth/recovery``) — публичный: войти по recovery-коду;
- ``recovery_router`` (``/passkey/recovery-codes``) — под JWT: сколько кодов осталось, перевыпуск.

Человек без Telegram, Google и почты получает аккаунт и 8 recovery-кодов. У такого аккаунта
нет ни почты, ни Telegram: уведомления ему приходят только в кабинет. Триала, кампаний и
реферальных бонусов при регистрации нет, пока их не включит настройка.
"""

from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.cabinet.auth import passkey as passkey_core, recovery_codes
from app.cabinet.auth.passkey import (
    PasskeyChallengeError,
    PasskeyUnavailableError,
    PasskeyVerificationError,
)
from app.cabinet.auth.passkey_throttle import enforce_passkey_signup_throttle
from app.config import settings
from app.database.crud.user import create_user_by_passkey, emit_user_created
from app.database.crud.user_passkey import get_passkey_by_credential_id
from app.database.crud.user_recovery_code import replace_recovery_codes
from app.database.models import UserPasskey
from app.services import legal_consent_service
from app.utils.cache import RateLimitCache

from ..dependencies import get_cabinet_db
from ..ip_utils import get_client_ip
from ..schemas.passkey import (
    PasskeyOptionsResponse,
    PasskeySignupOptionsRequest,
    PasskeySignupResponse,
    PasskeySignupVerifyRequest,
)
from .auth import (
    _create_auth_response,
    _process_campaign_bonus,
    _process_referral_code,
    _require_legal_consent,
    _store_refresh_token,
)
from .passkey import _error, _too_many_requests


logger = structlog.get_logger(__name__)

SIGNUP_SOURCE = 'cabinet_passkey'

signup_router = APIRouter(prefix='/auth/passkey/signup', tags=['Cabinet Passkey'])
recovery_auth_router = APIRouter(prefix='/auth/recovery', tags=['Cabinet Passkey'])
recovery_router = APIRouter(prefix='/passkey/recovery-codes', tags=['Cabinet Passkey'])


async def require_passkey_signup_open() -> None:
    if not settings.is_passkey_signup_enabled():
        raise _error(status.HTTP_404_NOT_FOUND, 'passkey_signup_disabled', 'Passkey sign-up is not enabled')
    if settings.INVITE_ONLY_ENABLED:
        # Приглашение привязано к личности (Telegram, почта), а у passkey-регистрации её нет.
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={'code': 'registration_invite_required'})


# ---------------------------------------------------------------------------
# Регистрация аккаунта ключом
# ---------------------------------------------------------------------------


@signup_router.post(
    '/options', response_model=PasskeyOptionsResponse, dependencies=[Depends(require_passkey_signup_open)]
)
async def passkey_signup_options(
    raw_request: Request,
    request: PasskeySignupOptionsRequest | None = None,
) -> PasskeyOptionsResponse:
    client_ip = get_client_ip(raw_request)
    if await RateLimitCache.is_ip_rate_limited(
        client_ip, 'passkey_signup_options', limit=20, window=60, fail_closed=True
    ):
        raise _too_many_requests()

    try:
        challenge_id, options = await passkey_core.create_signup_options(
            display_name=request.display_name if request else None
        )
    except PasskeyUnavailableError as exc:
        raise _error(
            status.HTTP_503_SERVICE_UNAVAILABLE, exc.code, 'Passkey service is temporarily unavailable'
        ) from exc
    return PasskeyOptionsResponse(challenge_id=challenge_id, options=options)


@signup_router.post(
    '/verify', response_model=PasskeySignupResponse, dependencies=[Depends(require_passkey_signup_open)]
)
async def passkey_signup_verify(
    request: PasskeySignupVerifyRequest,
    raw_request: Request,
    response: Response,
    db: AsyncSession = Depends(get_cabinet_db),
) -> PasskeySignupResponse:
    client_ip = get_client_ip(raw_request)
    # Считаем каждую попытку, не только удачную: лимит нужен против подбора и накрутки аккаунтов.
    await enforce_passkey_signup_throttle(client_ip)

    # Согласие проверяем ДО траты челленджа: на 428 фронт рисует галочки и повторяет запрос
    # с тем же ответом аутентификатора, ключ на устройстве заново создавать не придётся.
    consent_documents = await _require_legal_consent(
        db, accepted=request.accepted_legal_documents, language=settings.DEFAULT_LANGUAGE
    )

    try:
        spent = await passkey_core.consume_signup_challenge(request.challenge_id)
    except PasskeyChallengeError as exc:
        raise _error(status.HTTP_400_BAD_REQUEST, exc.code, 'Challenge is invalid or expired') from exc

    try:
        new_key = passkey_core.verify_registration(challenge=spent.challenge, credential=request.credential)
    except PasskeyVerificationError as exc:
        raise _error(status.HTTP_400_BAD_REQUEST, exc.code, 'Passkey could not be verified') from exc

    if await get_passkey_by_credential_id(db, new_key.credential_id) is not None:
        raise _credential_taken()

    shown_codes, code_hashes = _fresh_codes()
    try:
        user = await create_user_by_passkey(
            db, display_name=spent.display_name, language=settings.DEFAULT_LANGUAGE, emit_event=False
        )
        db.add(
            UserPasskey(
                user_id=user.id,
                credential_id=new_key.credential_id,
                public_key=new_key.public_key,
                sign_count=new_key.sign_count,
                device_type=new_key.device_type,
                backed_up=new_key.backed_up,
                transports=new_key.transports,
                name=request.name or 'Passkey 1',
                user_handle=spent.user_handle,
            )
        )
        await replace_recovery_codes(db, user.id, code_hashes)
        await legal_consent_service.record_consent(
            db, user, consent_documents, source=SIGNUP_SOURCE, ip_address=client_ip, commit=False
        )
        user.cabinet_last_login = datetime.now(UTC)
        await db.commit()
    except IntegrityError as exc:
        # Гонка: тот же credential_id успели занять между проверкой и записью.
        await db.rollback()
        logger.warning('Passkey signup refused: credential id already taken')
        raise _credential_taken() from exc
    except Exception:
        await db.rollback()
        raise

    logger.info('Account created by passkey', user_id=user.id)
    await emit_user_created(db, user)

    auth = await _create_auth_response(user, db)
    await _store_refresh_token(db, user.id, auth.refresh_token)

    if settings.PASSKEY_SIGNUP_BONUSES_ENABLED:
        auth.campaign_bonus = await _process_campaign_bonus(db, user, request.campaign_slug)
        await _process_referral_code(db, user, request.referral_code, is_new_user=True)

    # Открытые коды в ответе: ни браузер, ни прокси не должны их запоминать.
    response.headers['Cache-Control'] = 'no-store'
    return PasskeySignupResponse.model_validate({**auth.model_dump(), 'recovery_codes': shown_codes})


def _credential_taken() -> HTTPException:
    return _error(status.HTTP_409_CONFLICT, 'passkey_already_registered', 'This passkey is already registered')


def _fresh_codes() -> tuple[list[str], list[str]]:
    """Новый набор: (коды для показа человеку, их хэши для БД)."""
    pairs = recovery_codes.new_code_set()
    return [shown for shown, _ in pairs], [digest for _, digest in pairs]
