"""Passkey (WebAuthn) в кабинете.

Два роутера:
- ``auth_router`` (``/auth/passkey``) — публичный: статус и вход по passkey;
- ``router`` (``/passkey``) — под JWT: добавить, показать, переименовать, удалить ключ.

Passkey — дополнительный способ входа: ключ заводится в профиле уже после первого
входа. Логика WebAuthn — в ``app.cabinet.auth.passkey``; здесь HTTP, лимиты и БД.
"""

from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.cabinet.auth import passkey as passkey_core
from app.cabinet.auth.passkey import (
    PURPOSE_LOGIN,
    PURPOSE_REGISTER,
    PasskeyChallengeError,
    PasskeyUnavailableError,
    PasskeyVerificationError,
)
from app.config import settings
from app.database.auth_methods import compute_auth_methods
from app.database.crud.user import get_user_by_id
from app.database.crud.user_passkey import (
    count_user_passkeys,
    get_passkey_by_credential_id,
    get_user_passkey,
    list_user_passkeys,
    lock_user_row,
    record_assertion,
)
from app.database.models import User, UserPasskey, UserStatus
from app.utils.cache import RateLimitCache

from ..dependencies import get_cabinet_db, get_current_cabinet_user
from ..ip_utils import get_client_ip
from ..schemas.auth import AuthResponse
from ..schemas.passkey import (
    PasskeyDeleteResponse,
    PasskeyListResponse,
    PasskeyOptionsResponse,
    PasskeyRegisterVerifyRequest,
    PasskeyRenameRequest,
    PasskeyResponse,
    PasskeyStatusResponse,
    PasskeyVerifyRequest,
)
from .auth import _create_auth_response, _store_refresh_token


logger = structlog.get_logger(__name__)

MAX_PASSKEYS_PER_USER = 10

auth_router = APIRouter(prefix='/auth/passkey', tags=['Cabinet Passkey'])
router = APIRouter(prefix='/passkey', tags=['Cabinet Passkey'])


def _error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={'code': code, 'message': message})


def _login_failed() -> HTTPException:
    """Единственный ответ на любую причину отказа во входе: тело не подсказывает, что именно не так."""
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Passkey authentication failed')


def _too_many_requests() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail='Too many requests',
        headers={'Retry-After': '60'},
    )


async def require_passkey_enabled() -> None:
    if not settings.is_passkey_enabled():
        raise _error(status.HTTP_404_NOT_FOUND, 'passkey_disabled', 'Passkey authentication is not enabled')


def _to_response(passkey: UserPasskey) -> PasskeyResponse:
    return PasskeyResponse(
        id=passkey.id,
        name=passkey.name,
        device_type=passkey.device_type,
        backed_up=bool(passkey.backed_up),
        transports=[item for item in (passkey.transports or '').split(',') if item],
        created_at=passkey.created_at,
        last_used_at=passkey.last_used_at,
    )


def _user_name_for(user: User) -> str:
    return user.email or user.username or f'user-{user.id}'


# ---------------------------------------------------------------------------
# Публичные: статус и вход
# ---------------------------------------------------------------------------


@auth_router.get('/status', response_model=PasskeyStatusResponse)
async def passkey_status() -> PasskeyStatusResponse:
    """Нужен фронту, чтобы показать или спрятать кнопку «Войти с passkey»."""
    return PasskeyStatusResponse(enabled=settings.is_passkey_enabled())


@auth_router.post(
    '/login/options', response_model=PasskeyOptionsResponse, dependencies=[Depends(require_passkey_enabled)]
)
async def passkey_login_options(raw_request: Request) -> PasskeyOptionsResponse:
    client_ip = get_client_ip(raw_request)
    if await RateLimitCache.is_ip_rate_limited(
        client_ip, 'passkey_login_options', limit=20, window=60, fail_closed=True
    ):
        raise _too_many_requests()

    try:
        challenge_id, options = await passkey_core.create_login_options()
    except PasskeyUnavailableError as exc:
        raise _error(
            status.HTTP_503_SERVICE_UNAVAILABLE, exc.code, 'Passkey service is temporarily unavailable'
        ) from exc
    return PasskeyOptionsResponse(challenge_id=challenge_id, options=options)


@auth_router.post('/login/verify', response_model=AuthResponse, dependencies=[Depends(require_passkey_enabled)])
async def passkey_login_verify(
    request: PasskeyVerifyRequest,
    raw_request: Request,
    db: AsyncSession = Depends(get_cabinet_db),
) -> AuthResponse:
    client_ip = get_client_ip(raw_request)
    if await RateLimitCache.is_ip_rate_limited(
        client_ip, 'passkey_login_verify', limit=10, window=60, fail_closed=True
    ):
        raise _too_many_requests()

    # Челлендж тратится первым и при любом исходе: неудачная попытка не даёт второй.
    try:
        challenge = await passkey_core.consume_challenge(request.challenge_id, purpose=PURPOSE_LOGIN)
        credential_id = passkey_core.credential_id_of(request.credential)
    except (PasskeyChallengeError, PasskeyVerificationError) as exc:
        logger.info('Passkey login refused', reason=exc.code, client_ip=client_ip)
        raise _login_failed() from exc

    stored = await get_passkey_by_credential_id(db, credential_id)
    if stored is None:
        logger.info('Passkey login refused', reason='unknown_credential', client_ip=client_ip)
        raise _login_failed()

    # После rollback ORM-объекты протухают, а обращение к ним в async-сессии падает: ids — заранее.
    stored_user_id = stored.user_id
    seen_sign_count = int(stored.sign_count or 0)
    try:
        verified = passkey_core.verify_login(
            challenge=challenge,
            credential=request.credential,
            public_key=bytes(stored.public_key),
            sign_count=seen_sign_count,
        )
    except PasskeyVerificationError as exc:
        logger.warning('Passkey login refused', reason=exc.code, user_id=stored_user_id, client_ip=client_ip)
        raise _login_failed() from exc

    # Аутентификатор, вернувший userHandle другого аккаунта, — не наш ключ.
    if verified.user_handle is not None and verified.user_handle != passkey_core.user_handle_for(stored_user_id):
        logger.warning('Passkey login refused', reason='user_handle_mismatch', user_id=stored_user_id)
        raise _login_failed()

    user = await get_user_by_id(db, stored_user_id)
    # Статус проверяется ПОСЛЕ подписи и отвечает тем же 401, что и все остальные отказы: иначе
    # владелец валидного ключа узнавал бы по ответу, что аккаунт удалён или заблокирован.
    if user is None or user.status != UserStatus.ACTIVE.value:
        logger.warning('Passkey login refused', reason='inactive_user', user_id=stored_user_id)
        raise _login_failed()

    recorded = await record_assertion(
        db,
        stored,
        seen_sign_count=seen_sign_count,
        new_sign_count=verified.sign_count,
        backed_up=verified.backed_up,
    )
    if not recorded:
        await db.rollback()
        logger.warning('Passkey login refused', reason='concurrent_sign_count_update', user_id=stored_user_id)
        raise _login_failed()

    user.cabinet_last_login = datetime.now(UTC)
    await db.commit()

    response = await _create_auth_response(user, db)
    await _store_refresh_token(db, user.id, response.refresh_token)
    return response


# ---------------------------------------------------------------------------
# Под JWT: свои ключи
# ---------------------------------------------------------------------------


async def _enforce_user_rate_limit(user: User, action: str) -> None:
    if await RateLimitCache.is_rate_limited(user.id, action, limit=10, window=60, fail_closed=True):
        raise _too_many_requests()


@router.post(
    '/register/options', response_model=PasskeyOptionsResponse, dependencies=[Depends(require_passkey_enabled)]
)
async def passkey_register_options(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> PasskeyOptionsResponse:
    await _enforce_user_rate_limit(user, 'passkey_register_options')

    existing = await list_user_passkeys(db, user.id)
    if len(existing) >= MAX_PASSKEYS_PER_USER:
        raise _error(status.HTTP_400_BAD_REQUEST, 'passkey_limit_reached', 'Maximum number of passkeys reached')

    try:
        challenge_id, options = await passkey_core.create_registration_options(
            user_id=user.id,
            user_name=_user_name_for(user),
            display_name=user.first_name,
            existing=existing,
        )
    except PasskeyUnavailableError as exc:
        raise _error(
            status.HTTP_503_SERVICE_UNAVAILABLE, exc.code, 'Passkey service is temporarily unavailable'
        ) from exc
    return PasskeyOptionsResponse(challenge_id=challenge_id, options=options)


@router.post(
    '/register/verify',
    response_model=PasskeyResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_passkey_enabled)],
)
async def passkey_register_verify(
    request: PasskeyRegisterVerifyRequest,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> PasskeyResponse:
    await _enforce_user_rate_limit(user, 'passkey_register_verify')
    user_id = user.id  # после rollback ORM-объект протухает

    try:
        challenge = await passkey_core.consume_challenge(
            request.challenge_id, purpose=PURPOSE_REGISTER, user_id=user_id
        )
    except PasskeyChallengeError as exc:
        raise _error(status.HTTP_400_BAD_REQUEST, exc.code, 'Challenge is invalid or expired') from exc

    try:
        new_key = passkey_core.verify_registration(challenge=challenge, credential=request.credential)
    except PasskeyVerificationError as exc:
        raise _error(status.HTTP_400_BAD_REQUEST, exc.code, 'Passkey could not be verified') from exc

    await lock_user_row(db, user_id)
    count = await count_user_passkeys(db, user_id)
    if count >= MAX_PASSKEYS_PER_USER:
        await db.rollback()
        raise _error(status.HTTP_400_BAD_REQUEST, 'passkey_limit_reached', 'Maximum number of passkeys reached')

    record = UserPasskey(
        user_id=user_id,
        credential_id=new_key.credential_id,
        public_key=new_key.public_key,
        sign_count=new_key.sign_count,
        device_type=new_key.device_type,
        backed_up=new_key.backed_up,
        transports=new_key.transports,
        name=request.name or f'Passkey {count + 1}',
    )
    db.add(record)
    try:
        await db.commit()
    except IntegrityError as exc:
        # credential_id уникален глобально: чужой (или уже свой) ключ занять нельзя.
        await db.rollback()
        logger.warning('Passkey registration refused: credential id already taken', user_id=user_id)
        raise _error(
            status.HTTP_409_CONFLICT, 'passkey_already_registered', 'This passkey is already registered'
        ) from exc
    await db.refresh(record)

    logger.info('Passkey registered', user_id=user_id, passkey_id=record.id, device_type=record.device_type)
    return _to_response(record)


@router.get('', response_model=PasskeyListResponse, dependencies=[Depends(require_passkey_enabled)])
async def passkey_list(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> PasskeyListResponse:
    items = await list_user_passkeys(db, user.id)
    return PasskeyListResponse(passkeys=[_to_response(item) for item in items], limit=MAX_PASSKEYS_PER_USER)


@router.patch('/{passkey_id}', response_model=PasskeyResponse, dependencies=[Depends(require_passkey_enabled)])
async def passkey_rename(
    passkey_id: int,
    request: PasskeyRenameRequest,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> PasskeyResponse:
    record = await get_user_passkey(db, user.id, passkey_id)
    if record is None:
        raise _error(status.HTTP_404_NOT_FOUND, 'passkey_not_found', 'Passkey not found')

    record.name = request.name
    await db.commit()
    await db.refresh(record)
    return _to_response(record)


@router.delete('/{passkey_id}', response_model=PasskeyDeleteResponse, dependencies=[Depends(require_passkey_enabled)])
async def passkey_delete(
    passkey_id: int,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> PasskeyDeleteResponse:
    user_id = user.id
    await lock_user_row(db, user_id)
    record = await get_user_passkey(db, user_id, passkey_id)
    if record is None:
        await db.rollback()
        raise _error(status.HTTP_404_NOT_FOUND, 'passkey_not_found', 'Passkey not found')

    other_passkeys = await count_user_passkeys(db, user_id, excluding_id=record.id)
    if not compute_auth_methods(user) and other_passkeys == 0:
        await db.rollback()
        raise _error(
            status.HTTP_400_BAD_REQUEST,
            'last_login_method',
            'Cannot delete the last way to sign in to this account',
        )

    await db.delete(record)
    await db.commit()
    logger.info('Passkey deleted', user_id=user_id, passkey_id=passkey_id)
    return PasskeyDeleteResponse(success=True)
