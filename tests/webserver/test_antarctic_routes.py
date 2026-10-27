"""Публичная поверхность Antarctic Wallet: страница мини-приложения, сессия и статус, страница оплаты, вебхук.

Всё здесь доступно без авторизации, поэтому проверяем границы: подпись вебхука по сырому телу
до разбора JSON, подлинность кошелька по idToken, лимит запросов, экранирование и то, что секреты,
idToken и подпись не попадают в логи и в тела ответов.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import structlog
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.config import settings
from app.external.antarctic import AntarcticClient, AntarcticTokenError
from app.services.payment.antarctic import ANTARCTIC_TERMINAL_FAILURES, AntarcticSessionError
from app.webserver import antarctic as antarctic_module, payments as payments_module
from app.webserver.antarctic import create_antarctic_router
from app.webserver.payments import create_payment_router


APP_ID = '10000001'
APP_SECRET = 'sk_antarctic_test_secret'
TOKEN = 'Ab3_-' + 'x' * 27  # как secrets.token_urlsafe(24): 32 символа [A-Za-z0-9_-]
ID_TOKEN = 'eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ4In0.id-token-signature-marker'
LOGIN = 'wallet-login-1'
REFERRAL = 'https://app.antarcticwallet.com/r/friend'
SIGNATURE = 'deadbeef' * 8
STATIC_DIR = antarctic_module.STATIC_DIR

# sha256 файлов dist/ из npm-пакета @antarctic-wallet/aw-sdk@0.4.3 (integrity sha512-sPkLpB5N...).
VENDORED_SDK_SHA256 = {
    'aw-sdk.js': 'd73642ffa862fa8b3f67aa0f215978541028e88cd0a7696a3f08fd71f3172add',
    'sdk-CvaelF4s.js': 'b31c10890c2624ae0e02aa32510f282ee463e694f487fcb372bdc68bfb46b4be',
}


class FakeClient:
    """Подмена AntarcticClient: проверка idToken и подписи вебхука."""

    def __init__(self) -> None:
        self.sub = LOGIN
        self.token_error = False
        self.webhook_ok = True
        self.id_tokens: list[str] = []
        self.webhooks: list[tuple[bytes, str | None, str | None]] = []

    async def verify_id_token(self, id_token: str) -> str:
        self.id_tokens.append(id_token)
        if self.token_error:
            raise AntarcticTokenError('rejected')
        return self.sub

    def verify_webhook(self, raw_body: bytes, timestamp: str | None, signature: str | None) -> bool:
        self.webhooks.append((raw_body, timestamp, signature))
        return self.webhook_ok


class FakeService:
    """Подмена PaymentService с методами Antarctic."""

    def __init__(self) -> None:
        self.session_calls: list[tuple[str, str]] = []
        self.session_error: Exception | None = None
        self.session_result: dict = {
            'operation_id': 'op-1',
            'amount_usd': '5.43',
            'amount_kopeks': 50000,
            'status': 'pending',
            'is_paid': False,
        }
        self.refresh_calls: list[int] = []
        self.refresh_result: dict | None = None

    async def start_antarctic_session(self, db, *, token: str, id_token: str) -> dict:
        self.session_calls.append((token, id_token))
        if self.session_error is not None:
            raise self.session_error
        return self.session_result

    async def refresh_antarctic_payment(self, db, local_payment_id: int) -> dict | None:
        self.refresh_calls.append(local_payment_id)
        return self.refresh_result


def make_payment(**overrides) -> SimpleNamespace:
    values = {
        'id': 7,
        'token': TOKEN,
        'amount_kopeks': 50000,
        'amount_usd': '5.43',
        'status': 'pending',
        'is_paid': False,
        'user_app_login': None,
        'operation_id': None,
        'expires_at': datetime.now(UTC) + timedelta(minutes=30),
        'description': 'Пополнение баланса',
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    for key, value in {
        'ANTARCTIC_ENABLED': True,
        'ANTARCTIC_APP_ID': APP_ID,
        'ANTARCTIC_APP_SECRET': APP_SECRET,
        'ANTARCTIC_MINIAPP_PATH': '/antarctic/app',
        'ANTARCTIC_PAY_PAGE_PATH': '/antarctic/pay',
        'ANTARCTIC_WEBHOOK_PATH': '/antarctic-webhook',
        'ANTARCTIC_APP_LINK_TEMPLATE': 'https://t.me/antarctic_wallet_bot/app?startapp=app_{app_id}_{token}',
        'ANTARCTIC_REFERRAL_URL': REFERRAL,
        'ANTARCTIC_PAYMENT_TTL_MINUTES': 30,
        'WEBHOOK_URL': 'https://bot.example.com',
    }.items():
        monkeypatch.setattr(settings, key, value, raising=False)

    client = FakeClient()
    service = FakeService()
    payments: dict[str, SimpleNamespace] = {}
    lookups: list[str] = []
    limiter = SimpleNamespace(
        ip_calls=[],
        token_calls=[],
        counts={},
        force_ip=False,
        force_token=False,
        error=None,
    )
    callbacks: list[tuple[object, dict, str]] = []
    callback = SimpleNamespace(result=True, error=None)

    @asynccontextmanager
    async def fake_session_local():
        yield SimpleNamespace(name='db')

    async def fake_get_by_token(db, token: str):
        lookups.append(token)
        return payments.get(token)

    def hit(key, limit: int) -> bool:
        # Как настоящий счётчик: окно не истекает в тесте, лимит превышен с (limit + 1)-го вызова.
        limiter.counts[key] = limiter.counts.get(key, 0) + 1
        return limiter.counts[key] > limit

    async def fake_is_ip_rate_limited(ip, action, limit, window, **kwargs):
        limiter.ip_calls.append((ip, action, limit, window))
        if limiter.error is not None:
            raise limiter.error
        return limiter.force_ip or hit(('ip', ip, action), limit)

    async def forbidden_is_rate_limited(*args, **kwargs):
        # Лимит по IP идёт через is_ip_rate_limited; пользовательский ключ 0 тут не нужен.
        raise AssertionError('RateLimitCache.is_rate_limited must not be used for the Antarctic IP limit')

    async def fake_is_subject_rate_limited(subject, action, limit, window, **kwargs):
        limiter.token_calls.append((subject, action, limit, window))
        if limiter.error is not None:
            raise limiter.error
        return limiter.force_token or hit(('subject', subject, action), limit)

    async def fake_process_callback(payment_service, payload, method_name):
        callbacks.append((payment_service, payload, method_name))
        if callback.error is not None:
            raise callback.error
        return callback.result

    monkeypatch.setattr(antarctic_module, 'get_antarctic_client', lambda: client)
    monkeypatch.setattr(antarctic_module, 'AsyncSessionLocal', fake_session_local)
    monkeypatch.setattr(antarctic_module.antarctic_crud, 'get_antarctic_payment_by_token', fake_get_by_token)
    monkeypatch.setattr(antarctic_module.RateLimitCache, 'is_ip_rate_limited', staticmethod(fake_is_ip_rate_limited))
    monkeypatch.setattr(antarctic_module.RateLimitCache, 'is_rate_limited', staticmethod(forbidden_is_rate_limited))
    monkeypatch.setattr(
        antarctic_module.RateLimitCache,
        'is_subject_rate_limited',
        staticmethod(fake_is_subject_rate_limited),
    )
    monkeypatch.setattr(payments_module, '_process_payment_service_callback', fake_process_callback)

    app = FastAPI()
    app.include_router(create_antarctic_router(service))
    return SimpleNamespace(
        http=TestClient(app),
        client=client,
        service=service,
        payments=payments,
        lookups=lookups,
        limiter=limiter,
        callbacks=callbacks,
        callback=callback,
        monkeypatch=monkeypatch,
    )


def _session(env, body=None, *, raw: bytes | None = None):
    payload = {'idToken': ID_TOKEN, 'token': TOKEN} if body is None else body
    content = raw if raw is not None else json.dumps(payload).encode()
    return env.http.post('/antarctic/app/session', content=content)


def _status(env, body=None, *, raw: bytes | None = None):
    payload = {'idToken': ID_TOKEN, 'token': TOKEN} if body is None else body
    content = raw if raw is not None else json.dumps(payload).encode()
    return env.http.post('/antarctic/app/status', content=content)


# ---------------------------------------------------------------- страница мини-приложения


def test_miniapp_page_served_with_app_id_and_frame_policy(env) -> None:
    response = env.http.get('/antarctic/app')

    assert response.status_code == 200
    assert response.headers['content-type'].startswith('text/html')
    assert APP_ID in response.text
    assert 'aw-sdk.js' in response.text
    assert response.headers['content-security-policy'] == (
        'frame-ancestors https://antarcticwallet.com https://*.antarcticwallet.com https://web.telegram.org'
    )
    assert response.headers['cache-control'] == 'no-store'
    assert 'x-frame-options' not in response.headers
    assert '{{' not in response.text


def test_miniapp_frame_policy_allows_telegram_web(env) -> None:
    """Кошелёк открывается и в Telegram Web: без его origin в frame-ancestors страница не встраивается."""
    policy = env.http.get('/antarctic/app').headers['content-security-policy']

    assert policy.startswith('frame-ancestors ')
    assert 'https://web.telegram.org' in policy.split()
    assert '*' not in policy.split()


def test_miniapp_page_takes_failed_statuses_from_the_server(env) -> None:
    """Набор финальных неуспехов один и тот же у сервера и у страницы: он приходит в атрибуте <html>."""
    text = env.http.get('/antarctic/app').text

    match = re.search(r'<html[^>]*\sdata-failed-statuses="([^"]*)"', text)
    assert match is not None
    assert set(match.group(1).split(',')) == set(ANTARCTIC_TERMINAL_FAILURES)
    assert 'dataset.failedStatuses' in text


def _script_function(text: str, name: str) -> str:
    match = re.search(rf'(?:async )?function {name}\(.*?\n  \}}\n', text, re.DOTALL)
    assert match is not None, name
    return match.group(0)


def test_miniapp_script_stops_on_a_failed_session(env) -> None:
    """Сессия вернула rejected/failed/expired/amount_mismatch: кнопку «Оплатить» не показываем.

    JS-раннера в тестах нет, поэтому проверяем сам текст сценария: проверка финальных статусов
    стоит в ``openSession`` до того, как он разрешит показать оплату.
    """
    text = env.http.get('/antarctic/app').text

    body = _script_function(text, 'openSession')
    assert 'FAILED_STATUSES.has(session.status)' in body
    assert 'showFailed()' in body
    assert body.index('FAILED_STATUSES.has(session.status)') < body.index('return true')

    failed = _script_function(text, 'showFailed')
    assert 'Платёж не прошёл' in failed
    # тот же экран, что после проверки статуса при оплате
    assert 'showFailed()' in _script_function(text, 'pay')


def test_miniapp_page_has_no_external_scripts_and_the_agreed_copy(env) -> None:
    text = env.http.get('/antarctic/app').text

    assert re.findall(r'<script[^>]*\bsrc=', text) == []
    assert 'cdn' not in text.lower()
    for phrase in (
        'Откройте страницу в Antarctic Wallet',
        'Ссылка устарела — вернитесь в бот и создайте платёж заново',
        'Оплатить',
        'Оплата отменена',
        'Оплачено, баланс пополнен',
        'Платёж обрабатывается, баланс пополнится автоматически',
        'antarcticwallet.com',
        'user_rejected',
    ):
        assert phrase in text, phrase


def test_miniapp_page_escapes_configured_values(env) -> None:
    env.monkeypatch.setattr(settings, 'ANTARCTIC_APP_ID', '"><script>alert(1)</script>', raising=False)

    text = env.http.get('/antarctic/app').text

    assert '<script>alert(1)' not in text
    assert '&lt;script&gt;alert(1)' in text


def test_sdk_served_as_js(env) -> None:
    response = env.http.get('/antarctic/app/aw-sdk.js')

    assert response.status_code == 200
    assert response.headers['content-type'].split(';')[0] == 'application/javascript'
    assert response.headers['cache-control'] == 'public, max-age=86400'
    assert response.text.startswith('/*!')
    header = response.text.split('*/', 1)[0]
    assert 'MIT License' in header
    assert 'Copyright (c) 2026 Antarctic Tech' in header
    assert '0.4.3' in header
    assert 'AWSDK' in response.text


def test_every_module_the_sdk_imports_is_served(env) -> None:
    sdk = env.http.get('/antarctic/app/aw-sdk.js').text
    imports = re.findall(r'from\s+[\'"]\./([A-Za-z0-9._-]+\.js)[\'"]', sdk)
    assert imports, 'aw-sdk.js больше не импортирует соседний чанк — обновите тест и файлы'

    for name in imports:
        response = env.http.get(f'/antarctic/app/{name}')
        assert response.status_code == 200, name
        assert response.headers['content-type'].split(';')[0] == 'application/javascript'
        assert response.headers['cache-control'] == 'public, max-age=86400'


@pytest.mark.parametrize('name', sorted(VENDORED_SDK_SHA256))
def test_vendored_sdk_is_the_npm_package_unmodified(name: str) -> None:
    content = (STATIC_DIR / name).read_bytes()
    original = content.split(b'*/\n', 1)[1]

    assert hashlib.sha256(original).hexdigest() == VENDORED_SDK_SHA256[name]


@pytest.mark.parametrize(
    'name',
    ['nope.js', 'aw-sdk.js.map', 'app.html', 'pay.html', '..%2f..%2fconfig.py', '%2e%2e%2fapp.html', 'a%2fb.js'],
)
def test_only_vendored_scripts_are_served(env, name: str) -> None:
    response = env.http.get(f'/antarctic/app/{name}')

    assert response.status_code == 404
    assert 'MIT License' not in response.text


# ---------------------------------------------------------------- /session


@pytest.mark.parametrize(
    ('code', 'http_status'),
    [('invalid_token', 401), ('not_found', 404), ('expired', 410), ('login_mismatch', 409), ('provider_error', 502)],
)
def test_session_maps_errors(env, code: str, http_status: int) -> None:
    env.service.session_error = AntarcticSessionError(code)

    response = _session(env)

    assert response.status_code == http_status
    assert response.json() == {'error': code}


def test_session_insufficient_balance_returns_amount(env) -> None:
    env.service.session_error = AntarcticSessionError('insufficient_balance', amount_usd='1.24')

    response = _session(env)

    assert response.status_code == 422
    assert response.json() == {'error': 'insufficient_balance', 'amountUsd': '1.24'}


def test_session_success_returns_camel_case_body(env) -> None:
    response = _session(env)

    assert response.status_code == 200
    assert response.json() == {
        'operationId': 'op-1',
        'amountUsd': '5.43',
        'amountRub': '500.00',
        'status': 'pending',
        'isPaid': False,
    }
    assert env.service.session_calls == [(TOKEN, ID_TOKEN)]
    assert response.headers['cache-control'] == 'no-store'


def test_session_amount_rub_keeps_kopeks_exactly(env) -> None:
    env.service.session_result = {**env.service.session_result, 'amount_kopeks': 123456, 'is_paid': True}

    body = _session(env).json()

    assert body['amountRub'] == '1234.56'
    assert body['isPaid'] is True


@pytest.mark.parametrize(
    'raw',
    [
        b'not json',
        b'',
        b'[]',
        b'"text"',
        b'{}',
        json.dumps({'idToken': 1, 'token': TOKEN}).encode(),
        json.dumps({'idToken': ID_TOKEN, 'token': None}).encode(),
        json.dumps({'idToken': '', 'token': TOKEN}).encode(),
        json.dumps({'idToken': ID_TOKEN, 'token': ''}).encode(),
        json.dumps({'idToken': ID_TOKEN, 'token': 'bad token!'}).encode(),
        json.dumps({'idToken': ID_TOKEN, 'token': 'x' * 65}).encode(),
        json.dumps({'idToken': 'x' * 9000, 'token': TOKEN}).encode(),
        b'\xff\xfe\x00',
    ],
)
def test_session_malformed_body_is_400(env, raw: bytes) -> None:
    response = _session(env, raw=raw)

    assert response.status_code == 400
    assert response.json() == {'error': 'bad_request'}
    assert env.service.session_calls == []


def test_session_absurdly_nested_json_is_400_not_a_crash(env) -> None:
    response = _session(env, raw=b'[' * 8000 + b']' * 8000)

    assert response.status_code == 400
    assert response.json() == {'error': 'bad_request'}


def test_session_oversized_body_is_413_and_never_parsed(env) -> None:
    response = _session(env, raw=b'{"idToken": "' + b'a' * 50_000 + b'"}')

    assert response.status_code == 413
    assert response.json() == {'error': 'payload_too_large'}
    assert env.service.session_calls == []


def test_session_unexpected_failure_is_500_without_details(env) -> None:
    env.service.session_error = RuntimeError(f'boom {ID_TOKEN} {APP_SECRET}')

    response = _session(env)

    assert response.status_code == 500
    assert response.json() == {'error': 'server_error'}
    assert ID_TOKEN not in response.text
    assert APP_SECRET not in response.text


# ---------------------------------------------------------------- /status


def test_status_returns_state_for_the_owner_of_the_payment(env) -> None:
    payment = make_payment(user_app_login=LOGIN)
    env.payments[TOKEN] = payment
    env.service.refresh_result = {'payment': payment, 'status': 'approved', 'is_paid': True}

    response = _status(env)

    assert response.status_code == 200
    assert response.json() == {'status': 'approved', 'isPaid': True}
    assert env.service.refresh_calls == [payment.id]
    assert env.client.id_tokens == [ID_TOKEN]


def test_status_requires_same_login(env) -> None:
    env.payments[TOKEN] = make_payment(user_app_login='someone-else')

    response = _status(env)

    assert response.status_code == 409
    assert response.json() == {'error': 'login_mismatch'}
    assert env.service.refresh_calls == []


def test_status_for_a_payment_nobody_opened_is_409(env) -> None:
    env.payments[TOKEN] = make_payment(user_app_login=None)

    response = _status(env)

    assert response.status_code == 409
    assert env.service.refresh_calls == []


def test_status_with_bad_id_token_is_401_and_does_not_touch_the_database(env) -> None:
    env.client.token_error = True
    env.payments[TOKEN] = make_payment(user_app_login=LOGIN)

    response = _status(env)

    assert response.status_code == 401
    assert response.json() == {'error': 'invalid_token'}
    assert env.lookups == []
    assert env.service.refresh_calls == []


def test_status_unknown_token_is_404(env) -> None:
    response = _status(env)

    assert response.status_code == 404
    assert response.json() == {'error': 'not_found'}


def test_status_payment_vanished_during_refresh_is_404(env) -> None:
    env.payments[TOKEN] = make_payment(user_app_login=LOGIN)
    env.service.refresh_result = None

    response = _status(env)

    assert response.status_code == 404
    assert response.json() == {'error': 'not_found'}


@pytest.mark.parametrize('raw', [b'nope', b'[]', b'{}', json.dumps({'idToken': ID_TOKEN, 'token': 'a b'}).encode()])
def test_status_malformed_body_is_400(env, raw: bytes) -> None:
    response = _status(env, raw=raw)

    assert response.status_code == 400
    assert response.json() == {'error': 'bad_request'}
    assert env.client.id_tokens == []


def test_status_provider_hiccup_is_500_without_details(env) -> None:
    env.payments[TOKEN] = make_payment(user_app_login=LOGIN)

    async def boom(db, local_payment_id):
        raise RuntimeError(f'boom {ID_TOKEN}')

    env.service.refresh_antarctic_payment = boom

    response = _status(env)

    assert response.status_code == 500
    assert response.json() == {'error': 'server_error'}
    assert ID_TOKEN not in response.text


# ---------------------------------------------------------------- лимит запросов


OTHER_TOKEN = 'Zz9_-' + 'y' * 27
TOKEN_DIGEST = hashlib.sha256(TOKEN.encode()).hexdigest()


@pytest.mark.parametrize('call', [_session, _status])
def test_ip_rate_limited_429_before_the_body_is_read(env, call) -> None:
    env.limiter.force_ip = True

    response = call(env, raw=b'garbage that is not even json')

    assert response.status_code == 429
    assert response.json() == {'error': 'rate_limited'}
    assert env.limiter.ip_calls == [('testclient', 'antarctic', 120, 60)]
    assert env.limiter.token_calls == []
    assert env.client.id_tokens == []
    assert env.service.session_calls == []
    assert env.lookups == []


@pytest.mark.parametrize('call', [_session, _status])
def test_token_rate_limited_429_before_the_id_token_is_verified(env, call) -> None:
    env.limiter.force_token = True
    env.payments[TOKEN] = make_payment(user_app_login=LOGIN)

    response = call(env)

    assert response.status_code == 429
    assert response.json() == {'error': 'rate_limited'}
    assert env.limiter.token_calls == [(TOKEN_DIGEST, 'antarctic_token', 20, 60)]
    assert env.client.id_tokens == []
    assert env.service.session_calls == []
    assert env.service.refresh_calls == []
    assert env.lookups == []


def test_token_limit_triggers_at_the_21st_call_for_that_token_only(env) -> None:
    for token in (TOKEN, OTHER_TOKEN):
        env.payments[token] = make_payment(token=token, user_app_login=LOGIN)
    env.service.refresh_result = {'payment': env.payments[TOKEN], 'status': 'pending', 'is_paid': False}

    # /session и /status делят один бюджет на токен: десять и десять.
    first_twenty = [(_session if i % 2 == 0 else _status)(env).status_code for i in range(20)]
    assert first_twenty == [200] * 20

    assert _session(env).status_code == 429
    assert _status(env).status_code == 429

    other = {'idToken': ID_TOKEN, 'token': OTHER_TOKEN}
    assert _session(env, other).status_code == 200
    assert _status(env, other).status_code == 200


def test_ip_limit_triggers_at_the_121st_request(env) -> None:
    """Токены разные, поэтому срабатывает только лимит по адресу."""
    statuses = [_session(env, {'idToken': ID_TOKEN, 'token': f'tok{i:04d}'}).status_code for i in range(120)]
    assert statuses == [200] * 120

    blocked = _session(env, {'idToken': ID_TOKEN, 'token': 'tok9999'})
    assert blocked.status_code == 429
    assert blocked.json() == {'error': 'rate_limited'}
    assert _status(env, {'idToken': ID_TOKEN, 'token': 'tok9998'}).status_code == 429
    assert len(env.service.session_calls) == 120


def test_token_limiter_key_is_a_digest_not_the_token(env) -> None:
    _session(env)

    assert [call[0] for call in env.limiter.token_calls] == [TOKEN_DIGEST]
    assert TOKEN not in repr(env.limiter.token_calls)
    assert TOKEN not in repr(env.limiter.ip_calls)


@pytest.mark.parametrize('call', [_session, _status])
def test_token_limiter_is_skipped_for_malformed_credentials(env, call) -> None:
    response = call(env, {'idToken': ID_TOKEN, 'token': 'bad token!'})

    assert response.status_code == 400
    assert env.limiter.token_calls == []


@pytest.mark.parametrize('call', [_session, _status])
def test_limiter_failure_does_not_block_payments(env, call) -> None:
    env.limiter.error = RuntimeError('redis is down')
    env.payments[TOKEN] = make_payment(user_app_login=LOGIN)
    env.service.refresh_result = {'payment': env.payments[TOKEN], 'status': 'pending', 'is_paid': False}

    response = call(env)

    assert response.status_code == 200
    assert env.limiter.ip_calls
    assert env.limiter.token_calls


def test_limiter_uses_the_client_address_from_trusted_headers_only(env) -> None:
    env.monkeypatch.setattr(settings, 'CABINET_TRUSTED_PROXIES', '', raising=False)

    _session(env)
    env.http.post(
        '/antarctic/app/session',
        content=json.dumps({'idToken': ID_TOKEN, 'token': TOKEN}),
        headers={'X-Forwarded-For': '198.51.100.9'},
    )

    assert [call[0] for call in env.limiter.ip_calls] == ['testclient', 'testclient']


def _proxy_warnings(logs: list[dict]) -> list[dict]:
    return [entry for entry in logs if entry['log_level'] == 'warning' and 'CABINET_TRUSTED_PROXIES' in entry['event']]


def test_warns_once_at_mount_when_no_trusted_proxies_are_set(env) -> None:
    env.monkeypatch.setattr(settings, 'CABINET_TRUSTED_PROXIES', '', raising=False)

    with structlog.testing.capture_logs() as logs:
        create_payment_router(DummyBot(), SimpleNamespace())

    warnings = _proxy_warnings(logs)
    assert len(warnings) == 1
    assert 'per proxy IP' in warnings[0]['event']


def test_no_proxy_warning_when_trusted_proxies_are_set(env) -> None:
    env.monkeypatch.setattr(settings, 'CABINET_TRUSTED_PROXIES', '127.0.0.1,10.0.0.0/8', raising=False)

    with structlog.testing.capture_logs() as logs:
        create_payment_router(DummyBot(), SimpleNamespace())

    assert _proxy_warnings(logs) == []


def test_no_proxy_warning_when_antarctic_is_not_configured(env) -> None:
    env.monkeypatch.setattr(settings, 'CABINET_TRUSTED_PROXIES', '', raising=False)
    env.monkeypatch.setattr(settings, 'ANTARCTIC_APP_SECRET', None, raising=False)

    with structlog.testing.capture_logs() as logs:
        create_payment_router(DummyBot(), SimpleNamespace())

    assert _proxy_warnings(logs) == []


def test_content_length_with_non_ascii_digits_is_not_trusted() -> None:
    """``'²'.isdigit()`` истинно, а ``int('²')`` падает: заголовок должен проверяться как ASCII."""
    body = b'{"a": 1}'

    async def receive() -> dict:
        return {'type': 'http.request', 'body': body, 'more_body': False}

    scope = {
        'type': 'http',
        'asgi': {'version': '3.0'},
        'method': 'POST',
        'path': '/antarctic/app/session',
        'headers': [(b'content-length', '²'.encode('latin-1'))],
    }

    assert asyncio.run(antarctic_module._read_body(Request(scope, receive), 1024)) == body


# ---------------------------------------------------------------- страница оплаты


def _pay(env, token: str = TOKEN):
    return env.http.get(f'/antarctic/pay/{token}')


def test_pay_page_renders_links_and_escapes(env) -> None:
    env.payments[TOKEN] = make_payment(description='<script>alert(1)</script> & "q"')

    response = _pay(env)

    assert response.status_code == 200
    assert response.headers['content-type'].startswith('text/html')
    text = response.text
    assert '<title>Оплата через Antarctic Wallet</title>' in text
    assert '$5.43' in text
    assert 'Баланс пополнится на 500\u00a0₽' in text
    assert f'href="https://t.me/antarctic_wallet_bot/app?startapp=app_{APP_ID}_{TOKEN}"' in text
    assert 'Оплатить в Antarctic Wallet' in text
    assert f'href="{REFERRAL}"' in text
    assert 'Нет кошелька? Создайте за минуту — пополнение по СБП' in text
    assert 'Создать кошелёк' in text
    assert 'Ссылка действует 30 минут' in text
    assert 'Списание со счёта кошелька, а не с карты. Проверьте, что на счёте есть $5.43.' in text
    assert '<script' not in text.lower()
    assert '&lt;script&gt;alert(1)&lt;/script&gt; &amp; &quot;q&quot;' in text


def test_pay_page_without_referral_has_no_referral_block(env) -> None:
    env.monkeypatch.setattr(settings, 'ANTARCTIC_REFERRAL_URL', None, raising=False)
    env.payments[TOKEN] = make_payment()

    text = _pay(env).text

    assert f'startapp=app_{APP_ID}_{TOKEN}' in text
    assert 'Создать кошелёк' not in text
    assert 'Нет кошелька' not in text


def test_pay_page_ignores_a_non_https_referral(env) -> None:
    env.monkeypatch.setattr(settings, 'ANTARCTIC_REFERRAL_URL', 'javascript:alert(1)', raising=False)
    env.payments[TOKEN] = make_payment()

    text = _pay(env).text

    assert 'javascript:' not in text
    assert 'Создать кошелёк' not in text


def test_pay_page_unknown_token_is_404(env) -> None:
    response = _pay(env, 'doesnotexist' + 'a' * 20)

    assert response.status_code == 404
    assert response.headers['content-type'].startswith('text/html')
    assert 'Платёж не найден' in response.text
    assert 'Оплатить в Antarctic Wallet' not in response.text


@pytest.mark.parametrize('token', ['x' * 65, 'a b', 'a%3Cb', 'caf%C3%A9'])
def test_pay_page_malformed_token_is_404_without_a_database_lookup(env, token: str) -> None:
    response = _pay(env, token)

    assert response.status_code == 404
    assert env.lookups == []


def test_pay_page_paid_shows_paid_state(env) -> None:
    env.payments[TOKEN] = make_payment(is_paid=True, status='approved')

    response = _pay(env)

    assert response.status_code == 200
    assert 'Оплачено' in response.text
    assert 'Оплатить в Antarctic Wallet' not in response.text
    assert f'startapp=app_{APP_ID}_{TOKEN}' not in response.text


@pytest.mark.parametrize('status', ['rejected', 'failed', 'expired', 'amount_mismatch'])
def test_pay_page_terminal_failure_shows_stale_link(env, status: str) -> None:
    env.payments[TOKEN] = make_payment(status=status)

    response = _pay(env)

    assert response.status_code == 410
    assert 'Ссылка устарела, создайте платёж заново' in response.text
    assert 'Оплатить в Antarctic Wallet' not in response.text
    assert f'startapp=app_{APP_ID}_{TOKEN}' not in response.text


def test_pay_page_unopened_payment_past_its_ttl_is_stale(env) -> None:
    env.payments[TOKEN] = make_payment(expires_at=datetime.now(UTC) - timedelta(minutes=1))

    response = _pay(env)

    assert response.status_code == 410
    assert 'Ссылка устарела, создайте платёж заново' in response.text


def test_pay_page_opened_payment_stays_payable_past_the_ttl(env) -> None:
    """Раз интент в AW уже есть, срок не отменяет возможный approved (решение R6)."""
    env.payments[TOKEN] = make_payment(
        expires_at=datetime.now(UTC) - timedelta(minutes=5),
        operation_id='op-1',
        user_app_login=LOGIN,
    )

    response = _pay(env)

    assert response.status_code == 200
    assert 'Оплатить в Antarctic Wallet' in response.text


def test_pay_page_headers_protect_the_token_in_the_url(env) -> None:
    env.payments[TOKEN] = make_payment()

    headers = _pay(env).headers

    assert headers['cache-control'] == 'no-store'
    assert headers['referrer-policy'] == 'no-referrer'
    assert headers['x-content-type-options'] == 'nosniff'
    assert "default-src 'none'" in headers['content-security-policy']
    assert "frame-ancestors 'none'" in headers['content-security-policy']


@pytest.mark.parametrize(
    ('minutes', 'phrase'),
    [(1, '1 минуту'), (2, '2 минуты'), (5, '5 минут'), (11, '11 минут'), (21, '21 минуту'), (30, '30 минут')],
)
def test_pay_page_footnote_follows_the_configured_ttl(env, minutes: int, phrase: str) -> None:
    env.monkeypatch.setattr(settings, 'ANTARCTIC_PAYMENT_TTL_MINUTES', minutes, raising=False)
    env.payments[TOKEN] = make_payment()

    assert f'Ссылка действует {phrase}' in _pay(env).text


def test_pay_page_formats_big_amounts(env) -> None:
    env.payments[TOKEN] = make_payment(amount_kopeks=123456, amount_usd='1234.56')

    text = _pay(env).text

    assert '1\u00a0234,56\u00a0₽' in text
    assert '$1234.56' in text


# ---------------------------------------------------------------- вебхук


def _webhook(env, body: bytes = b'{"event": "intent.approved"}', headers: dict | None = None):
    return env.http.post(
        '/antarctic-webhook',
        content=body,
        headers={
            'X-Sdk-Webhook-Timestamp': '1790000000',
            'X-Sdk-Webhook-Signature': SIGNATURE,
            **(headers or {}),
        },
    )


def test_webhook_bad_signature_401_before_parse(env) -> None:
    env.client.webhook_ok = False

    response = _webhook(env, body=b'this is { not json')

    assert response.status_code == 401
    assert response.json() == {'status': 'unauthorized'}
    assert env.callbacks == []
    assert env.client.webhooks == [(b'this is { not json', '1790000000', SIGNATURE)]


def test_webhook_without_signature_headers_is_401(env) -> None:
    env.client.webhook_ok = False

    response = env.http.post('/antarctic-webhook', content=b'{}')

    assert response.status_code == 401
    assert env.client.webhooks == [(b'{}', None, None)]
    assert env.callbacks == []


def test_webhook_valid_calls_service_200(env) -> None:
    body = b'{"event": "intent.approved", "data": {"referenceId": "x"}}'

    response = _webhook(env, body=body)

    assert response.status_code == 200
    assert response.json() == {'status': 'ok'}
    assert len(env.callbacks) == 1
    _, payload, method_name = env.callbacks[0]
    assert method_name == 'process_antarctic_webhook'
    assert payload == json.loads(body)
    assert env.client.webhooks[0][0] == body


def test_webhook_service_false_is_500(env) -> None:
    env.callback.result = False

    response = _webhook(env)

    assert response.status_code == 500
    assert response.json() == {'status': 'error'}


def test_webhook_service_exception_is_500(env) -> None:
    env.callback.error = RuntimeError('db is gone')

    response = _webhook(env)

    assert response.status_code == 500
    assert response.json() == {'status': 'error'}


@pytest.mark.parametrize('body', [b'not json', b'[1, 2]', b'"x"', b'', b'\xff\xfe'])
def test_webhook_signed_but_not_a_json_object_is_400(env, body: bytes) -> None:
    response = _webhook(env, body=body)

    assert response.status_code == 400
    assert response.json() == {'status': 'error'}
    assert env.callbacks == []


def test_webhook_signed_but_absurdly_nested_json_is_400_not_a_crash(env) -> None:
    response = _webhook(env, body=b'[' * 100_000 + b']' * 100_000)

    assert response.status_code == 400
    assert env.callbacks == []


def test_webhook_oversized_body_is_413_before_the_signature_check(env) -> None:
    response = _webhook(env, body=b'{"a": "' + b'x' * 300_000 + b'"}')

    assert response.status_code == 413
    assert env.client.webhooks == []
    assert env.callbacks == []


def test_webhook_without_a_client_is_503(env) -> None:
    env.monkeypatch.setattr(antarctic_module, 'get_antarctic_client', lambda: None)

    response = _webhook(env)

    assert response.status_code == 503
    assert env.callbacks == []


def test_webhook_health_reports_the_flag(env) -> None:
    response = env.http.get('/antarctic-webhook')

    assert response.status_code == 200
    assert response.json() == {'status': 'ok', 'service': 'antarctic_webhook', 'enabled': True}


def test_webhook_verifies_the_real_signature_over_the_raw_bytes(env) -> None:
    """Подпись считается по байтам как пришли: пробелы и порядок ключей важны, пересериализация их потеряла бы."""
    client = AntarcticClient(APP_ID, APP_SECRET, 'https://app.antarcticwallet.com')
    env.monkeypatch.setattr(antarctic_module, 'get_antarctic_client', lambda: client)

    body = b'{ "event" :"intent.approved",\n  "data": {"referenceId":"x"} }'
    timestamp = str(int(time.time()))
    message = f'{timestamp}.{hashlib.sha256(body).hexdigest()}'
    signature = hmac.new(APP_SECRET.encode(), message.encode(), hashlib.sha256).hexdigest()
    headers = {'X-Sdk-Webhook-Timestamp': timestamp, 'X-Sdk-Webhook-Signature': signature}

    assert _webhook(env, body=body, headers=headers).status_code == 200
    assert len(env.callbacks) == 1

    tampered = body.replace(b'intent.approved', b'intent.rejected')
    assert _webhook(env, body=tampered, headers=headers).status_code == 401
    assert len(env.callbacks) == 1


# ---------------------------------------------------------------- секреты не попадают в логи


def test_secrets_never_reach_the_logs(env) -> None:
    env.client.webhook_ok = False
    env.service.session_error = RuntimeError(f'boom {ID_TOKEN} {APP_SECRET} {SIGNATURE}')
    env.payments[TOKEN] = make_payment(user_app_login=LOGIN)

    async def boom(db, local_payment_id):
        raise RuntimeError(f'boom {ID_TOKEN} {SIGNATURE}')

    env.service.refresh_antarctic_payment = boom
    env.callback.error = RuntimeError(f'boom {SIGNATURE} {APP_SECRET}')

    with structlog.testing.capture_logs() as logs:
        _session(env)
        _status(env)
        _webhook(env)  # bad signature
        env.client.webhook_ok = True
        _webhook(env)  # valid signature, the service blows up

    dump = repr(logs)
    assert logs, 'ожидались записи в логе об отказах'
    for secret in (ID_TOKEN, APP_SECRET, SIGNATURE):
        assert secret not in dump


# ---------------------------------------------------------------- монтирование в платёжный роутер


class DummyBot:
    pass


def _paths(router) -> set[str]:
    """Пути роутера вместе с вложенными: FastAPI оборачивает include_router в _IncludedRouter без path."""
    paths: set[str] = set()
    for route in getattr(router, 'routes', []):
        nested = getattr(route, 'original_router', None)
        if nested is not None:
            paths |= _paths(nested)
        elif getattr(route, 'path', None):
            paths.add(route.path)
    return paths


ANTARCTIC_PATHS = {
    '/antarctic/app',
    '/antarctic/app/{asset_name}',
    '/antarctic/app/session',
    '/antarctic/app/status',
    '/antarctic/pay/{token}',
    '/antarctic-webhook',
}


@pytest.mark.parametrize('flag', [False, True])
def test_routes_are_mounted_by_credentials_not_by_the_enable_flag(env, flag: bool) -> None:
    env.monkeypatch.setattr(settings, 'ANTARCTIC_ENABLED', flag, raising=False)

    router = create_payment_router(DummyBot(), SimpleNamespace())

    assert router is not None
    assert {'/antarctic/app/session', '/antarctic/app/status', '/antarctic-webhook'} <= _paths(router)
    assert '/antarctic/pay/{token}' in _paths(router)
    assert '/antarctic/app' in _paths(router)


def test_unconfigured_antarctic_mounts_nothing(env) -> None:
    env.monkeypatch.setattr(settings, 'ANTARCTIC_APP_SECRET', None, raising=False)

    router = create_payment_router(DummyBot(), SimpleNamespace())

    paths = _paths(router) if router is not None else set()
    assert not (paths & ANTARCTIC_PATHS)
    assert not any('antarctic' in path for path in paths)


def test_payment_webhooks_health_lists_antarctic(env) -> None:
    router = create_payment_router(DummyBot(), SimpleNamespace())
    route = next(r for r in router.routes if getattr(r, 'path', '') == '/health/payment-webhooks')

    response = asyncio.run(route.endpoint())

    assert json.loads(response.body)['antarctic_enabled'] is True
