"""Passkey (WebAuthn) для кабинета: options → verify. Без FastAPI.

Тонкая обёртка над ``py_webauthn``: сама криптография и разбор структур — в
библиотеке, здесь политика (что требуем от аутентификатора, как хранится
челлендж, что считается отказом).

Политика:
- ``userVerification=required``, ``attestation=none``, резидентный ключ обязателен
  (вход без ввода логина — discoverable credentials).
- Челлендж одноразовый, живёт ``CHALLENGE_TTL_SECONDS`` в Redis, привязан к цели
  (регистрация, вход или создание аккаунта) и, для регистрации, к пользователю. Любая
  попытка его потратить — удачная или нет — сжигает его: подбирать нечего.
- Аккаунт, созданный самим passkey, на момент options ещё не существует, поэтому его
  userHandle случайный: он лежит в челлендже и после проверки уезжает в ``user_passkeys``.
- ``crossOrigin=true`` в clientDataJSON отвергается: кабинет не живёт в чужих iframe.
- Счётчик подписей: если хоть один из двух (сохранённый, присланный) ненулевой, новый
  обязан быть строго больше. Нулевой счётчик с обеих сторон — устройство без
  счётчика (iCloud Keychain, Google Password Manager), это норма.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import structlog
from webauthn import (
    base64url_to_bytes,
    generate_authentication_options,
    generate_registration_options,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import bytes_to_base64url, options_to_json_dict, parse_client_data_json
from webauthn.helpers.structs import (
    AttestationConveyancePreference,
    AuthenticatorSelectionCriteria,
    AuthenticatorTransport,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from app.config import settings
from app.utils.cache import cache, cache_key


logger = structlog.get_logger(__name__)

CHALLENGE_TTL_SECONDS = 300
#: Подсказка браузеру, сколько ждать пользователя; жёсткий предел — TTL челленджа.
CEREMONY_TIMEOUT_MS = 120_000

PURPOSE_REGISTER = 'register'
PURPOSE_LOGIN = 'login'
PURPOSE_SIGNUP = 'signup'

SIGNUP_USER_HANDLE_BYTES = 32

_CHALLENGE_ID_RE = re.compile(r'[A-Za-z0-9_-]{16,128}')
_TRANSPORTS = {item.value for item in AuthenticatorTransport}


class PasskeyError(Exception):
    """Базовая ошибка passkey; ``code`` уходит клиенту, текст — нет."""

    code = 'passkey_error'


class PasskeyChallengeError(PasskeyError):
    """Челлендж неизвестен, просрочен, уже потрачен или выдан не для этого запроса."""

    code = 'invalid_challenge'


class PasskeyVerificationError(PasskeyError):
    """Ответ аутентификатора не прошёл проверку."""

    code = 'verification_failed'


class PasskeyUnavailableError(PasskeyError):
    """Хранилище челленджей недоступно: выдать проверяемые options нельзя."""

    code = 'passkey_unavailable'


@dataclass(frozen=True)
class NewPasskey:
    """Проверенный результат регистрации — то, что кладётся в ``user_passkeys``."""

    credential_id: bytes
    public_key: bytes
    sign_count: int
    device_type: str
    backed_up: bool
    transports: str | None


@dataclass(frozen=True)
class VerifiedLogin:
    credential_id: bytes
    sign_count: int
    device_type: str
    backed_up: bool
    #: userHandle из ответа аутентификатора (если прислал); сверяется вызывающим с владельцем ключа.
    user_handle: bytes | None


@dataclass(frozen=True)
class SignupChallenge:
    """Потраченный signup-челлендж: его байты, userHandle аутентификатора и имя для нового аккаунта."""

    challenge: bytes
    user_handle: bytes
    display_name: str | None


def user_handle_for(user_id: int) -> bytes:
    """Непрозрачный стабильный идентификатор пользователя для аутентификатора.

    Не секрет и не ПДн: числовой id наружу не отдаём, но и от ротации ключей он не
    зависит — иначе после неё аутентификатор складывал бы дубли в список ключей.
    """
    return hashlib.sha256(b'bedolaga-cabinet-passkey-user:' + str(user_id).encode()).digest()


def _transports_list(raw: str | None) -> list[AuthenticatorTransport]:
    if not raw:
        return []
    return [AuthenticatorTransport(item) for item in raw.split(',') if item in _TRANSPORTS]


def _challenge_key(challenge_id: str) -> str:
    return cache_key('passkey', 'chal', challenge_id)


async def _store_challenge(
    challenge: bytes,
    *,
    purpose: str,
    user_id: int | None,
    user_handle: bytes | None = None,
    display_name: str | None = None,
) -> str:
    challenge_id = secrets.token_urlsafe(32)
    record: dict[str, Any] = {'challenge': bytes_to_base64url(challenge), 'purpose': purpose, 'user_id': user_id}
    if user_handle is not None:
        record['user_handle'] = bytes_to_base64url(user_handle)
    if display_name is not None:
        record['display_name'] = display_name
    stored = await cache.set(_challenge_key(challenge_id), record, expire=CHALLENGE_TTL_SECONDS)
    if not stored:
        raise PasskeyUnavailableError('challenge store is unavailable')
    return challenge_id


async def _take_challenge(challenge_id: str, *, purpose: str) -> dict:
    """Достать запись челленджа и уничтожить её в том же шаге; проверить, что он для этой церемонии."""
    if not isinstance(challenge_id, str) or not _CHALLENGE_ID_RE.fullmatch(challenge_id):
        raise PasskeyChallengeError('malformed challenge id')

    data = await cache.getdel(_challenge_key(challenge_id))
    if not isinstance(data, dict):
        raise PasskeyChallengeError('unknown or expired challenge')
    if data.get('purpose') != purpose:
        raise PasskeyChallengeError('challenge was issued for another ceremony')
    return data


def _challenge_bytes(data: dict) -> bytes:
    try:
        return base64url_to_bytes(data['challenge'])
    except Exception as exc:
        raise PasskeyChallengeError('corrupted challenge') from exc


async def consume_challenge(challenge_id: str, *, purpose: str, user_id: int | None = None) -> bytes:
    """Достать и ОДНОВРЕМЕННО уничтожить челлендж; вернуть его байты.

    ``user_id`` обязателен для регистрации: челлендж выдан конкретному пользователю.
    Уничтожается он до всех проверок — неудачная попытка тоже его тратит.
    Signup-челлендж отсюда не достать (``purpose`` не совпадёт): для него свой вход.
    """
    data = await _take_challenge(challenge_id, purpose=purpose)
    if purpose == PURPOSE_REGISTER and (user_id is None or data.get('user_id') != user_id):
        raise PasskeyChallengeError('challenge was issued to another user')
    return _challenge_bytes(data)


async def consume_signup_challenge(challenge_id: str) -> SignupChallenge:
    """Потратить signup-челлендж и вернуть его байты вместе с userHandle, выданным аутентификатору."""
    data = await _take_challenge(challenge_id, purpose=PURPOSE_SIGNUP)
    try:
        user_handle = base64url_to_bytes(data['user_handle'])
    except Exception as exc:
        raise PasskeyChallengeError('corrupted challenge') from exc
    if len(user_handle) != SIGNUP_USER_HANDLE_BYTES:
        raise PasskeyChallengeError('corrupted challenge')
    display_name = data.get('display_name')
    return SignupChallenge(
        challenge=_challenge_bytes(data),
        user_handle=user_handle,
        display_name=display_name if isinstance(display_name, str) else None,
    )


async def create_registration_options(
    *,
    user_id: int,
    user_name: str,
    display_name: str | None,
    existing: Iterable[Any],
) -> tuple[str, dict]:
    """Options для ``navigator.credentials.create()``; ``existing`` — уже заведённые ключи пользователя."""
    options = generate_registration_options(
        rp_id=settings.get_passkey_rp_id(),
        rp_name=settings.PASSKEY_RP_NAME,
        user_name=user_name,
        user_id=user_handle_for(user_id),
        user_display_name=display_name or user_name,
        timeout=CEREMONY_TIMEOUT_MS,
        attestation=AttestationConveyancePreference.NONE,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
        exclude_credentials=[
            PublicKeyCredentialDescriptor(id=bytes(item.credential_id), transports=_transports_list(item.transports))
            for item in existing
        ],
    )
    challenge_id = await _store_challenge(options.challenge, purpose=PURPOSE_REGISTER, user_id=user_id)
    return challenge_id, options_to_json_dict(options)


async def create_signup_options(*, display_name: str | None) -> tuple[str, dict]:
    """Options для создания аккаунта одним passkey: пользователя ещё нет, handle случайный.

    ``user.name`` — подпись в списке ключей браузера; у аккаунта нет ни почты, ни логина,
    поэтому подпись короткая и разная у каждого аккаунта (иначе несколько ключей одного
    сайта на устройстве не отличить).
    """
    user_handle = secrets.token_bytes(SIGNUP_USER_HANDLE_BYTES)
    user_name = f'passkey-{user_handle[:3].hex()}'
    options = generate_registration_options(
        rp_id=settings.get_passkey_rp_id(),
        rp_name=settings.PASSKEY_RP_NAME,
        user_name=user_name,
        user_id=user_handle,
        user_display_name=display_name or user_name,
        timeout=CEREMONY_TIMEOUT_MS,
        attestation=AttestationConveyancePreference.NONE,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
    )
    challenge_id = await _store_challenge(
        options.challenge, purpose=PURPOSE_SIGNUP, user_id=None, user_handle=user_handle, display_name=display_name
    )
    return challenge_id, options_to_json_dict(options)


async def create_login_options() -> tuple[str, dict]:
    """Options для ``navigator.credentials.get()`` без списка допустимых ключей (discoverable)."""
    options = generate_authentication_options(
        rp_id=settings.get_passkey_rp_id(),
        timeout=CEREMONY_TIMEOUT_MS,
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    challenge_id = await _store_challenge(options.challenge, purpose=PURPOSE_LOGIN, user_id=None)
    return challenge_id, options_to_json_dict(options)


def _require_dict(credential: Any) -> dict:
    if not isinstance(credential, dict):
        raise PasskeyVerificationError('credential is not an object')
    return credential


def _reject_cross_origin(credential: dict) -> None:
    try:
        client_data = parse_client_data_json(base64url_to_bytes(credential['response']['clientDataJSON']))
    except Exception as exc:
        raise PasskeyVerificationError('clientDataJSON is malformed') from exc
    if client_data.cross_origin:
        raise PasskeyVerificationError('cross-origin ceremonies are not accepted')


def credential_id_of(credential: Any) -> bytes:
    """credential id из ответа (rawId) — по нему вход находит ключ в БД."""
    raw_id = _require_dict(credential).get('rawId')
    if not isinstance(raw_id, str) or not raw_id:
        raise PasskeyVerificationError('credential has no rawId')
    try:
        decoded = base64url_to_bytes(raw_id)
    except Exception as exc:
        raise PasskeyVerificationError('rawId is not base64url') from exc
    if not decoded:
        raise PasskeyVerificationError('rawId is empty')
    return decoded


def verify_registration(*, challenge: bytes, credential: Any) -> NewPasskey:
    """Проверить ответ ``navigator.credentials.create()`` (fmt=none обычно, остальное проверяет библиотека)."""
    credential = _require_dict(credential)
    try:
        _reject_cross_origin(credential)
        verified = verify_registration_response(
            credential=credential,
            expected_challenge=challenge,
            expected_rp_id=settings.get_passkey_rp_id(),
            expected_origin=settings.get_passkey_origins(),
            require_user_presence=True,
            require_user_verification=True,
        )
        if credential_id_of(credential) != verified.credential_id:
            raise PasskeyVerificationError('credential id differs from the attested one')
        transports = [
            item
            for item in (credential.get('response') or {}).get('transports') or []
            if isinstance(item, str) and item in _TRANSPORTS
        ]
    except PasskeyError:
        raise
    except Exception as exc:
        logger.warning('Passkey registration rejected', reason=type(exc).__name__, detail=str(exc)[:200])
        raise PasskeyVerificationError('registration response did not verify') from exc

    return NewPasskey(
        credential_id=verified.credential_id,
        public_key=verified.credential_public_key,
        sign_count=verified.sign_count,
        device_type=verified.credential_device_type.value,
        backed_up=verified.credential_backed_up,
        transports=','.join(transports) or None,
    )


def verify_login(*, challenge: bytes, credential: Any, public_key: bytes, sign_count: int) -> VerifiedLogin:
    """Проверить подпись утверждения ``navigator.credentials.get()`` ключом пользователя."""
    credential = _require_dict(credential)
    try:
        _reject_cross_origin(credential)
        verified = verify_authentication_response(
            credential=credential,
            expected_challenge=challenge,
            expected_rp_id=settings.get_passkey_rp_id(),
            expected_origin=settings.get_passkey_origins(),
            credential_public_key=public_key,
            credential_current_sign_count=sign_count,
            require_user_verification=True,
        )
        handle = (credential.get('response') or {}).get('userHandle')
        user_handle = base64url_to_bytes(handle) if isinstance(handle, str) and handle else None
    except PasskeyError:
        raise
    except Exception as exc:
        logger.warning('Passkey assertion rejected', reason=type(exc).__name__, detail=str(exc)[:200])
        raise PasskeyVerificationError('assertion did not verify') from exc

    return VerifiedLogin(
        credential_id=verified.credential_id,
        sign_count=verified.new_sign_count,
        device_type=verified.credential_device_type.value,
        backed_up=verified.credential_backed_up,
        user_handle=user_handle,
    )
