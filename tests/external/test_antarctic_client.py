"""Tests for the Antarctic Wallet API client, idToken and webhook verification.

No test touches the network: every HTTP call goes through ``httpx.MockTransport``.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time
from collections.abc import Callable
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from structlog.testing import capture_logs

from app.external import antarctic
from app.external.antarctic import (
    AntarcticAPIError,
    AntarcticClient,
    AntarcticIntent,
    AntarcticTokenError,
    get_antarctic_client,
)


APP_ID = 'app-123'
APP_SECRET = 'super-secret-value-do-not-leak-0123456789'
API_BASE = 'https://aw.example.test'
JWKS_URL = f'{API_BASE}/api/v2/sdk/jwks'
KID = 'a' * 64
OTHER_KID = 'b' * 64


def _make_rsa_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwk_for(private_key: Any, *, kid: str = KID, alg: str = 'RS256', **overrides: Any) -> dict[str, Any]:
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    jwk.update({'kid': kid, 'alg': alg, 'use': 'sig'})
    jwk.update(overrides)
    return jwk


@pytest.fixture(scope='module')
def rsa_key() -> rsa.RSAPrivateKey:
    return _make_rsa_key()


@pytest.fixture(scope='module')
def other_rsa_key() -> rsa.RSAPrivateKey:
    return _make_rsa_key()


class JwksServer:
    """MockTransport handler that serves a mutable JWKS and counts fetches."""

    def __init__(self, keys: list[dict[str, Any]]) -> None:
        self.keys = keys
        self.fetches = 0
        self.status = 200

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert str(request.url) == JWKS_URL
        assert request.method == 'GET'
        self.fetches += 1
        if self.status != 200:
            return httpx.Response(self.status, json={'code': 'BOOM'})
        return httpx.Response(200, json={'keys': self.keys})


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> AntarcticClient:
    return AntarcticClient(APP_ID, APP_SECRET, API_BASE, transport=httpx.MockTransport(handler))


def _claims(**overrides: Any) -> dict[str, Any]:
    now = int(time.time())
    claims: dict[str, Any] = {'iss': 'aw', 'aud': APP_ID, 'sub': 'user-42', 'iat': now, 'exp': now + 600}
    claims.update(overrides)
    return {k: v for k, v in claims.items() if v is not None}


def _sign(private_key: Any, claims: dict[str, Any], *, kid: str | None = KID, alg: str = 'RS256') -> str:
    headers = {'kid': kid} if kid is not None else {}
    return jwt.encode(claims, private_key, algorithm=alg, headers=headers)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode()


def _hs256_token(secret: bytes, claims: dict[str, Any], *, kid: str = KID) -> str:
    """Hand-built HS256 token (PyJWT refuses PEM public keys as HMAC secrets)."""
    header = _b64url(json.dumps({'alg': 'HS256', 'typ': 'JWT', 'kid': kid}).encode())
    payload = _b64url(json.dumps(claims).encode())
    signing_input = f'{header}.{payload}'.encode()
    signature = _b64url(hmac.new(secret, signing_input, hashlib.sha256).digest())
    return f'{header}.{payload}.{signature}'


# --------------------------------------------------------------------------- intents


async def test_upsert_sends_contract() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={'operationId': 'op-1', 'status': 'PENDING', 'data': {'amount': '1.05'}},
        )

    intent = await _client(handler).upsert_pay_intent(
        user_app_login='user-42', reference_id='ref-token', amount_usd='1.05'
    )

    assert len(captured) == 1
    request = captured[0]
    assert request.method == 'POST'
    assert str(request.url) == f'{API_BASE}/api/apps/v1/intents'
    assert request.headers['X-Aw-App-Id'] == APP_ID
    assert request.headers['Authorization'] == f'Bearer {APP_SECRET}'
    assert json.loads(request.content) == {
        'type': 'pay',
        'user_app_login': 'user-42',
        'reference_id': 'ref-token',
        'data': {'amount': '1.05'},
    }
    assert intent == AntarcticIntent(
        operation_id='op-1',
        status='pending',
        amount='1.05',
        failure_reason=None,
        raw={'operationId': 'op-1', 'status': 'PENDING', 'data': {'amount': '1.05'}},
    )
    # `raw` is excluded from dataclass equality, so check it explicitly.
    assert intent.raw == {'operationId': 'op-1', 'status': 'PENDING', 'data': {'amount': '1.05'}}


async def test_upsert_parses_snake_case_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                'operation_id': 'op-2',
                'status': 'Failed',
                'failure_reason': 'INSUFFICIENT_FUNDS',
                'data': {'amount': '2.00'},
            },
        )

    intent = await _client(handler).upsert_pay_intent(user_app_login='u', reference_id='r', amount_usd='2.00')

    assert intent.operation_id == 'op-2'
    assert intent.status == 'failed'
    assert intent.failure_reason == 'INSUFFICIENT_FUNDS'
    assert intent.amount == '2.00'


async def test_upsert_parses_camel_case_failure_reason_without_data() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={'operationId': 'op-3', 'status': 'failed', 'failureReason': 'EXPIRED'})

    intent = await _client(handler).upsert_pay_intent(user_app_login='u', reference_id='r', amount_usd='2.00')

    assert intent.failure_reason == 'EXPIRED'
    assert intent.amount is None


def test_intent_is_hashable_and_hides_raw_in_repr() -> None:
    intent = AntarcticIntent('op-1', 'pending', '1.00', None, {'secret-ish': 'payload'})

    assert hash(intent) == hash(AntarcticIntent('op-1', 'pending', '1.00', None, {'other': 1}))
    assert 'payload' not in repr(intent)


async def test_upsert_error_raises_with_code() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={'code': 'USER_ID_NOT_ACCEPTED', 'message': 'nope'})

    with pytest.raises(AntarcticAPIError) as exc_info:
        await _client(handler).upsert_pay_intent(user_app_login='u', reference_id='r', amount_usd='1.00')

    assert exc_info.value.status_code == 422
    assert exc_info.value.code == 'USER_ID_NOT_ACCEPTED'
    assert APP_SECRET not in str(exc_info.value)
    assert APP_SECRET not in repr(exc_info.value)


async def test_upsert_error_reads_code_from_errors_list() -> None:
    # The shape AW actually returns (observed live, 2026-10-26).
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            json={'data': None, 'errors': ['USER_NOT_FOUND'], 'message': 'USER_NOT_FOUND', 'status': 'not_found'},
        )

    with pytest.raises(AntarcticAPIError) as exc_info:
        await _client(handler).upsert_pay_intent(user_app_login='u', reference_id='r', amount_usd='1.00')

    assert exc_info.value.status_code == 404
    assert exc_info.value.code == 'USER_NOT_FOUND'


async def test_upsert_error_without_json_body_has_no_code() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text='<html>bad gateway</html>')

    with pytest.raises(AntarcticAPIError) as exc_info:
        await _client(handler).upsert_pay_intent(user_app_login='u', reference_id='r', amount_usd='1.00')

    assert exc_info.value.status_code == 502
    assert exc_info.value.code is None


async def test_upsert_missing_operation_id_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={'status': 'pending'})

    with pytest.raises(AntarcticAPIError) as exc_info:
        await _client(handler).upsert_pay_intent(user_app_login='u', reference_id='r', amount_usd='1.00')

    assert exc_info.value.status_code == 200


@pytest.mark.parametrize('body', [b'not json', b'[1, 2, 3]', b'"str"'])
async def test_upsert_malformed_success_body_raises(body: bytes) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    with pytest.raises(AntarcticAPIError):
        await _client(handler).upsert_pay_intent(user_app_login='u', reference_id='r', amount_usd='1.00')


async def test_upsert_network_error_becomes_api_error_without_secret() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError('connection refused', request=request)

    with pytest.raises(AntarcticAPIError) as exc_info:
        await _client(handler).upsert_pay_intent(user_app_login='u', reference_id='r', amount_usd='1.00')

    assert exc_info.value.status_code is None
    assert exc_info.value.code is None
    assert APP_SECRET not in str(exc_info.value)


def test_client_repr_does_not_expose_secret() -> None:
    client = AntarcticClient(APP_ID, APP_SECRET, API_BASE)

    assert APP_SECRET not in repr(client)
    assert APP_SECRET not in str(client)


# --------------------------------------------------------------------------- idToken


async def test_id_token_valid_returns_sub(rsa_key: rsa.RSAPrivateKey) -> None:
    server = JwksServer([_jwk_for(rsa_key)])

    sub = await _client(server).verify_id_token(_sign(rsa_key, _claims()))

    assert sub == 'user-42'


async def test_id_token_accepts_audience_list(rsa_key: rsa.RSAPrivateKey) -> None:
    server = JwksServer([_jwk_for(rsa_key)])

    sub = await _client(server).verify_id_token(_sign(rsa_key, _claims(aud=['other-app', APP_ID])))

    assert sub == 'user-42'


async def test_id_token_wrong_aud(rsa_key: rsa.RSAPrivateKey) -> None:
    server = JwksServer([_jwk_for(rsa_key)])

    with pytest.raises(AntarcticTokenError):
        await _client(server).verify_id_token(_sign(rsa_key, _claims(aud='another-app')))


async def test_id_token_missing_aud(rsa_key: rsa.RSAPrivateKey) -> None:
    server = JwksServer([_jwk_for(rsa_key)])

    with pytest.raises(AntarcticTokenError):
        await _client(server).verify_id_token(_sign(rsa_key, _claims(aud=None)))


async def test_id_token_wrong_iss(rsa_key: rsa.RSAPrivateKey) -> None:
    server = JwksServer([_jwk_for(rsa_key)])

    with pytest.raises(AntarcticTokenError):
        await _client(server).verify_id_token(_sign(rsa_key, _claims(iss='evil')))


async def test_id_token_missing_iss(rsa_key: rsa.RSAPrivateKey) -> None:
    server = JwksServer([_jwk_for(rsa_key)])

    with pytest.raises(AntarcticTokenError):
        await _client(server).verify_id_token(_sign(rsa_key, _claims(iss=None)))


async def test_id_token_expired(rsa_key: rsa.RSAPrivateKey) -> None:
    server = JwksServer([_jwk_for(rsa_key)])
    now = int(time.time())

    with pytest.raises(AntarcticTokenError):
        await _client(server).verify_id_token(_sign(rsa_key, _claims(iat=now - 7200, exp=now - 3600)))


async def test_id_token_missing_exp(rsa_key: rsa.RSAPrivateKey) -> None:
    server = JwksServer([_jwk_for(rsa_key)])

    with pytest.raises(AntarcticTokenError):
        await _client(server).verify_id_token(_sign(rsa_key, _claims(exp=None)))


@pytest.mark.parametrize('sub', [None, ''])
async def test_id_token_missing_or_empty_sub(rsa_key: rsa.RSAPrivateKey, sub: str | None) -> None:
    server = JwksServer([_jwk_for(rsa_key)])

    with pytest.raises(AntarcticTokenError):
        await _client(server).verify_id_token(_sign(rsa_key, _claims(sub=sub)))


async def test_id_token_hs256_signed_with_secret(rsa_key: rsa.RSAPrivateKey) -> None:
    server = JwksServer([_jwk_for(rsa_key)])
    token = jwt.encode(_claims(), APP_SECRET, algorithm='HS256', headers={'kid': KID})

    with pytest.raises(AntarcticTokenError):
        await _client(server).verify_id_token(token)


async def test_id_token_hs256_signed_with_public_key_pem(rsa_key: rsa.RSAPrivateKey) -> None:
    """Classic algorithm-confusion attack: HMAC keyed with the (public) PEM."""
    server = JwksServer([_jwk_for(rsa_key)])
    pem = rsa_key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)

    with pytest.raises(AntarcticTokenError):
        await _client(server).verify_id_token(_hs256_token(pem, _claims()))


async def test_id_token_alg_none(rsa_key: rsa.RSAPrivateKey) -> None:
    server = JwksServer([_jwk_for(rsa_key)])
    header = _b64url(json.dumps({'alg': 'none', 'typ': 'JWT', 'kid': KID}).encode())
    payload = _b64url(json.dumps(_claims()).encode())

    with pytest.raises(AntarcticTokenError):
        await _client(server).verify_id_token(f'{header}.{payload}.')


async def test_id_token_alg_differs_from_jwk_alg(rsa_key: rsa.RSAPrivateKey) -> None:
    """Token says RS384 while the JWK declares RS256: the JWK wins, token rejected."""
    server = JwksServer([_jwk_for(rsa_key, alg='RS256')])

    with pytest.raises(AntarcticTokenError):
        await _client(server).verify_id_token(_sign(rsa_key, _claims(), alg='RS384'))


async def test_id_token_jwk_declaring_hmac_alg_is_ignored(rsa_key: rsa.RSAPrivateKey) -> None:
    server = JwksServer([_jwk_for(rsa_key, alg='HS256')])
    token = jwt.encode(_claims(), APP_SECRET, algorithm='HS256', headers={'kid': KID})

    with pytest.raises(AntarcticTokenError):
        await _client(server).verify_id_token(token)


async def test_id_token_oct_jwk_with_hmac_alg_is_rejected() -> None:
    """Even a symmetric JWK that matches an HS256 token must never verify anything."""
    jwk = {'kty': 'oct', 'kid': KID, 'alg': 'HS256', 'use': 'sig', 'k': _b64url(APP_SECRET.encode())}
    server = JwksServer([jwk])
    token = jwt.encode(_claims(), APP_SECRET, algorithm='HS256', headers={'kid': KID})

    with pytest.raises(AntarcticTokenError):
        await _client(server).verify_id_token(token)


async def test_id_token_jwk_without_alg_is_rejected(rsa_key: rsa.RSAPrivateKey) -> None:
    jwk = _jwk_for(rsa_key)
    del jwk['alg']
    server = JwksServer([jwk])

    with pytest.raises(AntarcticTokenError):
        await _client(server).verify_id_token(_sign(rsa_key, _claims()))


async def test_id_token_encryption_key_is_not_used_for_signatures(rsa_key: rsa.RSAPrivateKey) -> None:
    server = JwksServer([_jwk_for(rsa_key, use='enc')])

    with pytest.raises(AntarcticTokenError):
        await _client(server).verify_id_token(_sign(rsa_key, _claims()))


async def test_id_token_jwk_key_type_mismatch(rsa_key: rsa.RSAPrivateKey) -> None:
    """JWK is an EC key but declares RS256: key family must match the algorithm."""
    ec_key = ec.generate_private_key(ec.SECP256R1())
    jwk = jwt.algorithms.ECAlgorithm.to_jwk(ec_key.public_key(), as_dict=True)
    jwk.update({'kid': KID, 'alg': 'RS256', 'use': 'sig'})
    server = JwksServer([jwk])

    with pytest.raises(AntarcticTokenError):
        await _client(server).verify_id_token(_sign(rsa_key, _claims()))


async def test_id_token_es256_key_is_accepted() -> None:
    ec_key = ec.generate_private_key(ec.SECP256R1())
    jwk = jwt.algorithms.ECAlgorithm.to_jwk(ec_key.public_key(), as_dict=True)
    jwk.update({'kid': KID, 'alg': 'ES256', 'use': 'sig'})
    server = JwksServer([jwk])

    sub = await _client(server).verify_id_token(_sign(ec_key, _claims(), alg='ES256'))

    assert sub == 'user-42'


async def test_id_token_signed_by_other_key_with_known_kid(
    rsa_key: rsa.RSAPrivateKey, other_rsa_key: rsa.RSAPrivateKey
) -> None:
    server = JwksServer([_jwk_for(rsa_key)])

    with pytest.raises(AntarcticTokenError):
        await _client(server).verify_id_token(_sign(other_rsa_key, _claims()))


async def test_id_token_without_kid_is_rejected_without_fetching(rsa_key: rsa.RSAPrivateKey) -> None:
    server = JwksServer([_jwk_for(rsa_key)])

    with pytest.raises(AntarcticTokenError):
        await _client(server).verify_id_token(_sign(rsa_key, _claims(), kid=None))

    assert server.fetches == 0


@pytest.mark.parametrize('token', ['', 'garbage', 'a.b.c', 'x' * 100_000])
async def test_id_token_malformed(token: str) -> None:
    server = JwksServer([])

    with pytest.raises(AntarcticTokenError):
        await _client(server).verify_id_token(token)


async def test_id_token_error_does_not_leak_token_or_secret(rsa_key: rsa.RSAPrivateKey) -> None:
    server = JwksServer([_jwk_for(rsa_key)])
    token = _sign(rsa_key, _claims(aud='another-app'))

    with pytest.raises(AntarcticTokenError) as exc_info:
        await _client(server).verify_id_token(token)

    assert token not in str(exc_info.value)
    assert APP_SECRET not in str(exc_info.value)


async def test_jwks_fetch_failure_is_a_token_error(rsa_key: rsa.RSAPrivateKey) -> None:
    server = JwksServer([_jwk_for(rsa_key)])
    server.status = 503

    with pytest.raises(AntarcticTokenError):
        await _client(server).verify_id_token(_sign(rsa_key, _claims()))


@pytest.mark.parametrize('body', [{'keys': 'nope'}, {'nokeys': []}, ['x']])
async def test_jwks_malformed_document_is_a_token_error(rsa_key: rsa.RSAPrivateKey, body: Any) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    with pytest.raises(AntarcticTokenError):
        await _client(handler).verify_id_token(_sign(rsa_key, _claims()))


async def test_jwks_request_carries_no_credentials(rsa_key: rsa.RSAPrivateKey) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={'keys': [_jwk_for(rsa_key)]})

    await _client(handler).verify_id_token(_sign(rsa_key, _claims()))

    assert len(seen) == 1
    assert 'authorization' not in seen[0].headers
    assert 'x-aw-app-id' not in seen[0].headers


async def test_jwks_cached_and_refetched_on_unknown_kid(
    rsa_key: rsa.RSAPrivateKey, other_rsa_key: rsa.RSAPrivateKey
) -> None:
    server = JwksServer([_jwk_for(rsa_key)])
    client = _client(server)

    await client.verify_id_token(_sign(rsa_key, _claims()))
    await client.verify_id_token(_sign(rsa_key, _claims()))
    assert server.fetches == 1

    # AW rotates the key: the new kid appears in the JWKS only after the first miss.
    server.keys = [_jwk_for(rsa_key), _jwk_for(other_rsa_key, kid=OTHER_KID)]
    sub = await client.verify_id_token(_sign(other_rsa_key, _claims(), kid=OTHER_KID))

    assert sub == 'user-42'
    assert server.fetches == 2

    # The rotated key is now cached too.
    await client.verify_id_token(_sign(other_rsa_key, _claims(), kid=OTHER_KID))
    assert server.fetches == 2


async def test_unknown_kid_refetches_exactly_once_then_fails(rsa_key: rsa.RSAPrivateKey) -> None:
    server = JwksServer([_jwk_for(rsa_key)])
    client = _client(server)
    await client.verify_id_token(_sign(rsa_key, _claims()))
    assert server.fetches == 1

    with pytest.raises(AntarcticTokenError):
        await client.verify_id_token(_sign(rsa_key, _claims(), kid=OTHER_KID))

    assert server.fetches == 2


async def test_jwks_refetched_after_ttl(rsa_key: rsa.RSAPrivateKey) -> None:
    server = JwksServer([_jwk_for(rsa_key)])
    client = _client(server)
    token = _sign(rsa_key, _claims())

    await client.verify_id_token(token)
    client._jwks_fetched_at -= 3599
    await client.verify_id_token(token)
    assert server.fetches == 1

    client._jwks_fetched_at -= 2
    await client.verify_id_token(token)
    assert server.fetches == 2


async def test_unknown_kids_within_cooldown_cause_one_extra_fetch(rsa_key: rsa.RSAPrivateKey) -> None:
    server = JwksServer([_jwk_for(rsa_key)])
    client = _client(server)
    await client.verify_id_token(_sign(rsa_key, _claims()))
    assert server.fetches == 1

    for i in range(20):
        with pytest.raises(AntarcticTokenError):
            await client.verify_id_token(_sign(rsa_key, _claims(), kid=f'{i:064x}'))

    # The first unknown kid may refresh the JWKS (key rotation), the other 19 are rejected unfetched.
    assert server.fetches == 2


async def test_concurrent_unknown_kids_cause_one_extra_fetch(rsa_key: rsa.RSAPrivateKey) -> None:
    server = JwksServer([_jwk_for(rsa_key)])
    client = _client(server)
    await client.verify_id_token(_sign(rsa_key, _claims()))

    results = await asyncio.gather(
        *(client.verify_id_token(_sign(rsa_key, _claims(), kid=f'{i:064x}')) for i in range(20)),
        return_exceptions=True,
    )

    assert all(isinstance(result, AntarcticTokenError) for result in results)
    assert server.fetches == 2


async def test_unknown_kid_fetches_again_after_cooldown(
    rsa_key: rsa.RSAPrivateKey, other_rsa_key: rsa.RSAPrivateKey
) -> None:
    server = JwksServer([_jwk_for(rsa_key)])
    client = _client(server)
    await client.verify_id_token(_sign(rsa_key, _claims()))
    rotated = _sign(other_rsa_key, _claims(), kid=OTHER_KID)

    # AW has not published the new key yet: the forced refresh finds nothing.
    with pytest.raises(AntarcticTokenError):
        await client.verify_id_token(rotated)
    assert server.fetches == 2

    # The key appears, but we are inside the cooldown: rejected without a fetch.
    server.keys = [_jwk_for(rsa_key), _jwk_for(other_rsa_key, kid=OTHER_KID)]
    with pytest.raises(AntarcticTokenError):
        await client.verify_id_token(rotated)
    assert server.fetches == 2

    # Cooldown elapsed: exactly one more fetch, and the rotated key is picked up.
    client._jwks_last_forced_at -= 31
    assert await client.verify_id_token(rotated) == 'user-42'
    assert server.fetches == 3


async def test_known_kid_does_not_wait_for_inflight_refresh(
    rsa_key: rsa.RSAPrivateKey, other_rsa_key: rsa.RSAPrivateKey
) -> None:
    keys = [_jwk_for(rsa_key)]
    gate_closed = False
    entered = asyncio.Event()
    release = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        if gate_closed:
            entered.set()
            await release.wait()
        return httpx.Response(200, json={'keys': keys})

    client = _client(handler)
    known = _sign(rsa_key, _claims())
    await client.verify_id_token(known)

    # A rotation refresh starts and stalls on the network while holding the refresh lock.
    gate_closed = True
    keys.append(_jwk_for(other_rsa_key, kid=OTHER_KID))
    refresh = asyncio.create_task(client.verify_id_token(_sign(other_rsa_key, _claims(), kid=OTHER_KID)))
    await asyncio.wait_for(entered.wait(), timeout=2)

    # A login with a cached kid must complete right away instead of queueing behind it.
    assert await asyncio.wait_for(client.verify_id_token(known), timeout=2) == 'user-42'
    assert not refresh.done()

    release.set()
    assert await asyncio.wait_for(refresh, timeout=2) == 'user-42'


async def test_failed_jwks_fetch_is_not_retried_within_cooldown(rsa_key: rsa.RSAPrivateKey) -> None:
    server = JwksServer([_jwk_for(rsa_key)])
    server.status = 503
    client = _client(server)
    token = _sign(rsa_key, _claims())

    for _ in range(3):
        with pytest.raises(AntarcticTokenError):
            await client.verify_id_token(token)
    assert server.fetches == 1

    server.status = 200
    client._jwks_last_failure_at -= 31
    assert await client.verify_id_token(token) == 'user-42'
    assert server.fetches == 2


async def test_rejected_token_is_logged_at_debug_only(rsa_key: rsa.RSAPrivateKey) -> None:
    server = JwksServer([_jwk_for(rsa_key)])
    client = _client(server)

    with capture_logs() as records:
        with pytest.raises(AntarcticTokenError):
            await client.verify_id_token(_sign(rsa_key, _claims(aud='another-app')))

    rejected = [record for record in records if record['event'] == 'Antarctic idToken rejected']
    assert rejected
    assert all(record['log_level'] == 'debug' for record in rejected)
    assert not [record for record in records if record['log_level'] in {'warning', 'error'}]


# --------------------------------------------------------------------------- webhook


def _webhook_signature(secret: str, timestamp: str, body: bytes) -> str:
    message = f'{timestamp}.{hashlib.sha256(body).hexdigest()}'
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


def test_webhook_signature() -> None:
    client = AntarcticClient(APP_ID, APP_SECRET, API_BASE)
    body = b'{"event":"intent.completed","operationId":"op-1"}'
    now = 1_800_000_000.0
    timestamp = str(int(now))
    signature = _webhook_signature(APP_SECRET, timestamp, body)

    assert client.verify_webhook(body, timestamp, signature, now=now) is True
    # one byte of the body changed
    assert client.verify_webhook(body[:-1] + b'X', timestamp, signature, now=now) is False
    # hex digest case does not matter
    assert client.verify_webhook(body, timestamp, signature.upper(), now=now) is True
    # replay window: 300 s is still fine, 301 s is not (both directions)
    assert client.verify_webhook(body, timestamp, signature, now=now + 300) is True
    assert client.verify_webhook(body, timestamp, signature, now=now - 300) is True
    assert client.verify_webhook(body, timestamp, signature, now=now + 301) is False
    assert client.verify_webhook(body, timestamp, signature, now=now - 301) is False
    # missing headers
    assert client.verify_webhook(body, None, signature, now=now) is False
    assert client.verify_webhook(body, timestamp, None, now=now) is False
    assert client.verify_webhook(body, '', signature, now=now) is False
    assert client.verify_webhook(body, timestamp, '', now=now) is False


def test_webhook_signature_known_vector() -> None:
    """Fixed vector so that a regression in the signed message format is caught."""
    client = AntarcticClient(APP_ID, 'secret', API_BASE)
    body = b'{}'
    body_hash = hashlib.sha256(b'{}').hexdigest()
    assert body_hash == '44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a'
    expected = hmac.new(b'secret', f'1700000000.{body_hash}'.encode(), hashlib.sha256).hexdigest()

    assert client.verify_webhook(body, '1700000000', expected, now=1_700_000_000.0) is True


def test_webhook_signature_wrong_secret_or_timestamp_binding() -> None:
    client = AntarcticClient(APP_ID, APP_SECRET, API_BASE)
    body = b'{"a":1}'
    now = 1_800_000_000.0
    timestamp = str(int(now))

    assert client.verify_webhook(body, timestamp, _webhook_signature('other-secret', timestamp, body), now=now) is False
    # signature minted for another timestamp cannot be replayed with a fresh one
    old = _webhook_signature(APP_SECRET, str(int(now) - 10), body)
    assert client.verify_webhook(body, timestamp, old, now=now) is False


@pytest.mark.parametrize(
    'timestamp',
    [
        'abc',
        '1.5',
        '-1800000000',
        '+1800000000',
        '1_800_000_000',
        ' 1800000000',
        '1800000000 ',
        '9' * 5000,
        '１８００００００００',
    ],
)
def test_webhook_rejects_malformed_timestamp(timestamp: str) -> None:
    client = AntarcticClient(APP_ID, APP_SECRET, API_BASE)
    body = b'{}'
    signature = _webhook_signature(APP_SECRET, timestamp, body)

    assert client.verify_webhook(body, timestamp, signature, now=1_800_000_000.0) is False


@pytest.mark.parametrize('signature', ['zz', 'é' * 64, 'a' * 63, 'a' * 65, '\x00'])
def test_webhook_rejects_garbage_signature_without_raising(signature: str) -> None:
    client = AntarcticClient(APP_ID, APP_SECRET, API_BASE)

    assert client.verify_webhook(b'{}', '1800000000', signature, now=1_800_000_000.0) is False


def test_webhook_default_now_is_wall_clock() -> None:
    client = AntarcticClient(APP_ID, APP_SECRET, API_BASE)
    body = b'{}'
    timestamp = str(int(time.time()))
    signature = _webhook_signature(APP_SECRET, timestamp, body)

    assert client.verify_webhook(body, timestamp, signature) is True
    assert client.verify_webhook(body, str(int(time.time()) - 3600), signature) is False


# --------------------------------------------------------------------------- factory


@pytest.fixture
def reset_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(antarctic, '_client', None)
    monkeypatch.setattr(antarctic, '_client_key', None)


def _configure(monkeypatch: pytest.MonkeyPatch, app_id: str | None, secret: str | None, base: str = API_BASE) -> None:
    monkeypatch.setattr(antarctic.settings, 'ANTARCTIC_APP_ID', app_id)
    monkeypatch.setattr(antarctic.settings, 'ANTARCTIC_APP_SECRET', secret)
    monkeypatch.setattr(antarctic.settings, 'ANTARCTIC_API_BASE', base)


def test_get_client_none_when_not_configured(monkeypatch: pytest.MonkeyPatch, reset_factory: None) -> None:
    _configure(monkeypatch, None, None)
    assert get_antarctic_client() is None

    _configure(monkeypatch, APP_ID, None)
    assert get_antarctic_client() is None

    _configure(monkeypatch, None, APP_SECRET)
    assert get_antarctic_client() is None


def test_get_client_cached_and_rebuilt_on_change(monkeypatch: pytest.MonkeyPatch, reset_factory: None) -> None:
    _configure(monkeypatch, APP_ID, APP_SECRET)
    first = get_antarctic_client()
    assert first is not None
    assert get_antarctic_client() is first

    _configure(monkeypatch, APP_ID, 'rotated-secret')
    second = get_antarctic_client()
    assert second is not None and second is not first

    _configure(monkeypatch, 'other-app', 'rotated-secret')
    third = get_antarctic_client()
    assert third is not None and third is not second

    _configure(monkeypatch, 'other-app', 'rotated-secret', 'https://aw2.example.test/')
    fourth = get_antarctic_client()
    assert fourth is not None and fourth is not third

    _configure(monkeypatch, None, None)
    assert get_antarctic_client() is None
