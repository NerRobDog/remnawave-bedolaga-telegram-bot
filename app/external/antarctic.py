"""Client for the Antarctic Wallet (AW) app API.

Covers three things the payment provider needs:

* creating / updating pay intents (``POST /api/apps/v1/intents``);
* verifying the ``idToken`` that the AW mini-app hands to our page (RS/PS/ES/EdDSA
  signature against the AW JWKS, ``iss``/``aud``/``exp`` checked);
* verifying the HMAC signature of AW webhooks.

Secrets (``app_secret``), ``idToken`` values and webhook signatures are never logged and
never put into exception messages.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
import jwt
import structlog

from app.config import settings


logger = structlog.get_logger(__name__)


_INTENTS_PATH = '/api/apps/v1/intents'
_JWKS_PATH = '/api/v2/sdk/jwks'

_ID_TOKEN_ISSUER = 'aw'
# Tokens are a few hundred bytes; anything this large is garbage, do not even parse it.
_MAX_ID_TOKEN_LENGTH = 8192
# Tolerated clock drift between AW and this host for exp / nbf / iat.
_ID_TOKEN_LEEWAY_SECONDS = 10
_JWKS_TTL_SECONDS = 3600.0
# Unscheduled JWKS fetches (an unknown kid in a fresh cache, or a retry after a failed fetch) are
# throttled to one per interval: the endpoint that feeds us idTokens is unauthenticated, so
# without this anybody could make us call AW once per request.
_JWKS_UNSCHEDULED_FETCH_INTERVAL_SECONDS = 30.0
# Only asymmetric algorithms may ever verify an idToken: HS* would let anybody who knows the
# (public) key material forge tokens, `none` is no signature at all.
_ALLOWED_ID_TOKEN_ALGORITHMS = frozenset(
    {
        'RS256',
        'RS384',
        'RS512',
        'PS256',
        'PS384',
        'PS512',
        'ES256',
        'ES384',
        'ES512',
        'ES256K',
        'EdDSA',
    }
)

_WEBHOOK_TOLERANCE_SECONDS = 300
# A unix timestamp in seconds has 10 digits; the cap keeps int() away from absurd inputs.
_MAX_WEBHOOK_TIMESTAMP_LENGTH = 16


class AntarcticAPIError(Exception):
    """AW API call failed (non-2xx, network error or unusable response)."""

    def __init__(self, message: str, *, status_code: int | None = None, code: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code


class AntarcticTokenError(Exception):
    """idToken could not be verified (malformed, bad signature, wrong claims, JWKS unavailable)."""


@dataclass(frozen=True)
class AntarcticIntent:
    """Result of an intent upsert, normalised over AW's camelCase / snake_case field names."""

    operation_id: str
    status: str
    amount: str | None = None
    failure_reason: str | None = None
    # Kept for debugging only: excluded from repr and from equality / hash.
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)


def _first_present(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value is not None:
            return value
    return None


def _as_clean_str(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    return text or None


def _shape(value: Any, depth: int = 0) -> Any:
    """Structure of a JSON value with every leaf replaced by its type name."""
    if depth > 4:
        return '...'
    if isinstance(value, dict):
        return {str(key): _shape(item, depth + 1) for key, item in list(value.items())[:30]}
    if isinstance(value, list):
        return [_shape(value[0], depth + 1)] if value else []
    return type(value).__name__


def _extract_error_code(body: Any) -> str | None:
    """Pull AW's machine-readable error code out of an error body, if there is one."""
    if not isinstance(body, dict):
        return None
    code = body.get('code') or body.get('errorCode') or body.get('error_code')
    if code is None and isinstance(body.get('error'), dict):
        code = body['error'].get('code')
    # AW's actual shape: {"errors": ["USER_NOT_FOUND"], "message": "USER_NOT_FOUND", ...}
    if code is None and isinstance(body.get('errors'), list) and body['errors']:
        code = body['errors'][0]
    if code is None:
        code = body.get('message')
    return code if isinstance(code, str) and code else None


class AntarcticClient:
    """Async client for one AW app (``app_id`` + ``app_secret``)."""

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        api_base: str,
        *,
        timeout: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.app_id = app_id
        self._app_secret = app_secret
        self.api_base = api_base.rstrip('/')
        self._timeout = timeout
        self._transport = transport
        self._jwks: dict[str, dict[str, Any]] | None = None
        self._jwks_fetched_at = 0.0
        self._jwks_last_forced_at: float | None = None
        self._jwks_last_failure_at: float | None = None
        # Guards refreshes only; cache hits never touch it.
        self._jwks_lock = asyncio.Lock()

    def __repr__(self) -> str:
        return f'AntarcticClient(app_id={self.app_id!r}, api_base={self.api_base!r})'

    def _http(self) -> httpx.AsyncClient:
        # Redirects stay off (httpx default): a redirect must never carry the Bearer secret elsewhere.
        return httpx.AsyncClient(timeout=self._timeout, transport=self._transport)

    # ------------------------------------------------------------------ intents

    async def upsert_pay_intent(self, *, user_app_login: str, reference_id: str, amount_usd: str) -> AntarcticIntent:
        """Create (or update) the pay intent identified by ``reference_id`` for the AW user."""
        payload = {
            'type': 'pay',
            'user_app_login': user_app_login,
            'reference_id': reference_id,
            'data': {'amount': amount_usd},
        }
        headers = {
            'X-Aw-App-Id': self.app_id,
            'Authorization': f'Bearer {self._app_secret}',
            'Accept': 'application/json',
        }

        try:
            async with self._http() as http:
                response = await http.post(f'{self.api_base}{_INTENTS_PATH}', json=payload, headers=headers)
        except httpx.HTTPError as exc:
            # Class name only: transport error text is not ours to trust with a secret-bearing request.
            logger.warning('Antarctic intents request failed', error=type(exc).__name__)
            raise AntarcticAPIError(f'Antarctic API request failed: {type(exc).__name__}') from None

        try:
            body: Any = response.json()
        except ValueError:
            body = None

        if not response.is_success:
            code = _extract_error_code(body)
            logger.warning('Antarctic intents request rejected', status_code=response.status_code, code=code)
            suffix = f' ({code})' if code else ''
            raise AntarcticAPIError(
                f'Antarctic API error: HTTP {response.status_code}{suffix}',
                status_code=response.status_code,
                code=code,
            )

        return self._parse_intent(body, response.status_code)

    @staticmethod
    def _parse_intent(body: Any, status_code: int) -> AntarcticIntent:
        if not isinstance(body, dict):
            raise AntarcticAPIError('Antarctic API returned an unexpected response body', status_code=status_code)

        # AW wraps every response as {data, errors, message, status} (errors carry the code on failure);
        # on success the intent itself sits in data. A bare intent also has a data field (the amount),
        # so the envelope is recognised by its errors key.
        envelope = body
        if 'errors' in body and isinstance(body.get('data'), dict):
            body = body['data']

        operation_id = _as_clean_str(_first_present(body, 'operationId', 'operation_id'))
        if operation_id is None:
            # Field names and types only, never values: enough to fix the parser without leaking data.
            logger.warning(
                'Antarctic intents response has no operation id', status_code=status_code, shape=_shape(envelope)
            )
            raise AntarcticAPIError('Antarctic API response has no operation id', status_code=status_code)

        status = _as_clean_str(body.get('status'))
        data = body.get('data')
        amount = _as_clean_str(data.get('amount')) if isinstance(data, dict) else None
        failure_reason = _as_clean_str(_first_present(body, 'failureReason', 'failure_reason'))

        return AntarcticIntent(
            operation_id=operation_id,
            status=status.lower() if status else 'unknown',
            amount=amount,
            failure_reason=failure_reason,
            raw=body,
        )

    # ------------------------------------------------------------------ idToken

    async def verify_id_token(self, id_token: str) -> str:
        """Verify an AW idToken and return its ``sub``. Any failure raises ``AntarcticTokenError``."""
        try:
            return await self._verify_id_token(id_token)
        except AntarcticTokenError:
            raise
        except Exception as exc:
            # Fail closed: whatever went wrong (bad signature, bad claims, JWKS outage, odd key
            # material) the token is not trusted. Class name only, never the token itself. Debug level:
            # anybody can submit garbage tokens, that must not flood the warning log.
            logger.debug('Antarctic idToken rejected', reason=type(exc).__name__)
            raise AntarcticTokenError(f'idToken verification failed: {type(exc).__name__}') from None

    async def _verify_id_token(self, id_token: str) -> str:
        if not isinstance(id_token, str) or not id_token or len(id_token) > _MAX_ID_TOKEN_LENGTH:
            raise AntarcticTokenError('idToken is malformed')

        header = jwt.get_unverified_header(id_token)
        algorithm = header.get('alg')
        if algorithm not in _ALLOWED_ID_TOKEN_ALGORITHMS:
            raise AntarcticTokenError('idToken algorithm is not allowed')
        kid = header.get('kid')
        if not isinstance(kid, str) or not kid:
            raise AntarcticTokenError('idToken has no key id')

        jwk = await self._get_jwk(kid)
        if jwk is None:
            raise AntarcticTokenError('idToken signing key is unknown')
        # The key's own declaration decides the algorithm; the token header only has to agree.
        if jwk['alg'] != algorithm:
            raise AntarcticTokenError('idToken algorithm does not match the signing key')

        signing_key = jwt.PyJWK.from_dict(jwk, algorithm=algorithm)
        claims = jwt.decode(
            id_token,
            signing_key.key,
            algorithms=[algorithm],
            audience=self.app_id,
            issuer=_ID_TOKEN_ISSUER,
            leeway=_ID_TOKEN_LEEWAY_SECONDS,
            options={'require': ['exp', 'iss', 'aud', 'sub']},
        )

        subject = claims.get('sub')
        if not isinstance(subject, str) or not subject:
            raise AntarcticTokenError('idToken has no subject')
        return subject

    def _cached_jwk(self, kid: str, now: float) -> dict[str, Any] | None:
        """JWK for ``kid`` from a cache that is still within its TTL, else ``None``."""
        if self._jwks is None or now - self._jwks_fetched_at >= _JWKS_TTL_SECONDS:
            return None
        return self._jwks.get(kid)

    async def _get_jwk(self, kid: str) -> dict[str, Any] | None:
        """Return the signing JWK for ``kid``; refreshes the JWKS at most once per call and rate-limited."""
        # Fast path without the lock: a valid login never queues behind somebody else's refresh.
        jwk = self._cached_jwk(kid, time.monotonic())
        if jwk is not None:
            return jwk

        async with self._jwks_lock:
            now = time.monotonic()
            # Somebody may have refreshed while we waited for the lock.
            jwk = self._cached_jwk(kid, now)
            if jwk is not None:
                return jwk

            if self._jwks is not None and now - self._jwks_fetched_at < _JWKS_TTL_SECONDS:
                # Fresh cache, unknown kid: key rotation or garbage. One refresh per interval.
                if (
                    self._jwks_last_forced_at is not None
                    and now - self._jwks_last_forced_at < _JWKS_UNSCHEDULED_FETCH_INTERVAL_SECONDS
                ):
                    return None
                self._jwks_last_forced_at = now
            elif (
                self._jwks_last_failure_at is not None
                and now - self._jwks_last_failure_at < _JWKS_UNSCHEDULED_FETCH_INTERVAL_SECONDS
            ):
                # Empty / expired cache and the last fetch just failed: do not stack 15 s timeouts.
                raise AntarcticTokenError('JWKS is unavailable')

            await self._refresh_jwks()
            return self._jwks.get(kid) if self._jwks is not None else None

    async def _refresh_jwks(self) -> None:
        try:
            keys = await self._fetch_jwks()
        except AntarcticTokenError:
            self._jwks_last_failure_at = time.monotonic()
            raise
        self._jwks = keys
        self._jwks_fetched_at = time.monotonic()
        self._jwks_last_failure_at = None

    async def _fetch_jwks(self) -> dict[str, dict[str, Any]]:
        try:
            async with self._http() as http:
                response = await http.get(f'{self.api_base}{_JWKS_PATH}', headers={'Accept': 'application/json'})
            response.raise_for_status()
            document = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning('Antarctic JWKS fetch failed', error=type(exc).__name__)
            raise AntarcticTokenError('JWKS is unavailable') from None

        raw_keys = document.get('keys') if isinstance(document, dict) else None
        if not isinstance(raw_keys, list):
            raise AntarcticTokenError('JWKS document is malformed')

        keys: dict[str, dict[str, Any]] = {}
        for raw_key in raw_keys:
            if not isinstance(raw_key, dict):
                continue
            kid = raw_key.get('kid')
            # Only signature keys that declare an asymmetric algorithm are usable.
            if not isinstance(kid, str) or not kid:
                continue
            if raw_key.get('alg') not in _ALLOWED_ID_TOKEN_ALGORITHMS:
                continue
            if raw_key.get('use', 'sig') != 'sig':
                continue
            keys[kid] = raw_key

        return keys

    # ------------------------------------------------------------------ webhook

    def verify_webhook(
        self,
        raw_body: bytes,
        timestamp: str | None,
        signature: str | None,
        *,
        now: float | None = None,
    ) -> bool:
        """Check ``X-Sdk-Webhook-Timestamp`` / ``X-Sdk-Webhook-Signature`` against the raw request body.

        signature = hex(HMAC-SHA256(app_secret, f'{timestamp}.{sha256(raw_body).hexdigest()}')),
        and the timestamp must be within 300 s of ``now`` (replay protection).
        """
        if not timestamp or not signature:
            return False
        if len(timestamp) > _MAX_WEBHOOK_TIMESTAMP_LENGTH or not timestamp.isascii() or not timestamp.isdigit():
            return False

        current = time.time() if now is None else now
        if abs(current - int(timestamp)) > _WEBHOOK_TOLERANCE_SECONDS:
            return False

        message = f'{timestamp}.{hashlib.sha256(raw_body).hexdigest()}'
        expected = hmac.new(self._app_secret.encode('utf-8'), message.encode('utf-8'), hashlib.sha256).hexdigest()
        # Compare as bytes: str inputs to compare_digest must be ASCII or it raises TypeError.
        return hmac.compare_digest(expected.encode('ascii'), signature.lower().encode('utf-8'))


_client: AntarcticClient | None = None
_client_key: tuple[str, str, str] | None = None


def get_antarctic_client() -> AntarcticClient | None:
    """Shared client for the configured AW app; rebuilt when app id, secret or API base change."""
    global _client, _client_key

    if not settings.is_antarctic_configured():
        return None

    key = (
        settings.ANTARCTIC_APP_ID or '',
        settings.ANTARCTIC_APP_SECRET or '',
        settings.ANTARCTIC_API_BASE,
    )
    if _client is None or _client_key != key:
        _client = AntarcticClient(*key)
        _client_key = key
    return _client
