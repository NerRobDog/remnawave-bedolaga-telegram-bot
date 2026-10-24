"""Публичная поверхность Antarctic Wallet (AW): страница мини-приложения, сессия и статус, страница оплаты, вебхук.

Всё здесь доступно без авторизации, поэтому:

* кошелёк подтверждается подписанным ``idToken`` (``sub`` — логин кошелька), а не данными из тела запроса;
* подпись вебхука считается по сырым байтам тела и проверяется до разбора JSON;
* ``/session`` и ``/status`` ограничены по IP (до чтения тела) и по токену платежа (после разбора),
  а размер тел — по байтам, ещё до разбора;
* значения, попавшие на страницы, экранируются, в логи не идут ни ``idToken``, ни секрет, ни подпись,
  а тела ошибок несут только короткие коды.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse

from app.cabinet.ip_utils import get_client_ip
from app.config import settings
from app.database.crud import antarctic as antarctic_crud
from app.database.database import AsyncSessionLocal
from app.external.antarctic import AntarcticTokenError, get_antarctic_client
from app.services.payment.antarctic import ANTARCTIC_TERMINAL_FAILURES, AntarcticSessionError
from app.services.payment_service import PaymentService
from app.utils.cache import RateLimitCache
from app.webserver import payments as payments_module


logger = structlog.get_logger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / 'static' / 'antarctic'

# Кому разрешено встраивать страницу мини-приложения: сам кошелёк и Telegram Web, где он открывается.
MINIAPP_CSP = 'frame-ancestors https://antarcticwallet.com https://*.antarcticwallet.com https://web.telegram.org'
# Страница оплаты — обычная страница в браузере: скриптов нет, встраивать её никуда не нужно.
PAY_PAGE_CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; "
    "form-action 'none'; frame-ancestors 'none'"
)

# Лимиты общие для /session и /status. Платёж занимает около шести запросов.
# По IP: потолок на один адрес (NAT и общий IP прокси держат много людей, поэтому щедро).
# По токену платежа: один платёж не нужен больше двадцати запросов в минуту, а перебор или
# накрутка запросов к AW по одному токену упирается именно сюда, с какого бы адреса ни шёл.
RATE_LIMIT_IP_REQUESTS = 120
RATE_LIMIT_TOKEN_REQUESTS = 20
RATE_LIMIT_WINDOW_SECONDS = 60

# Тело /session и /status — idToken (до 8 КиБ) и токен платежа; вебхук AW — небольшой JSON.
MAX_JSON_BODY_BYTES = 16 * 1024
MAX_WEBHOOK_BODY_BYTES = 256 * 1024
MAX_ID_TOKEN_LENGTH = 8192

_TOKEN_RE = re.compile(r'[A-Za-z0-9_-]{1,64}')
_BLOCK_RE = re.compile(r'<!--if:(?P<name>\w+)-->\n?(?P<body>.*?)<!--endif:(?P=name)-->\n?', re.DOTALL)
_PLACEHOLDER_RE = re.compile(r'\{\{([A-Z_]+)\}\}')
_MAX_DESCRIPTION_LENGTH = 200

_SDK_CACHE_CONTROL = 'public, max-age=86400'
_PAGE_HEADERS = {'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'}


class _BodyTooLarge(Exception):
    pass


# ---------------------------------------------------------------- шаблоны и статика


@cache
def _template(name: str) -> str:
    return (STATIC_DIR / name).read_text(encoding='utf-8')


@cache
def _sdk_assets() -> dict[str, bytes]:
    """Вендоренные модули SDK: ``aw-sdk.js`` импортирует соседний чанк, отдаём все ``*.js`` каталога."""
    return {path.name: path.read_bytes() for path in sorted(STATIC_DIR.glob('*.js'))}


def _render(template: str, values: dict[str, str], blocks: frozenset[str] = frozenset()) -> str:
    """Подставляет значения в шаблон: блоки ``<!--if:x-->…<!--endif:x-->`` остаются по списку, значения экранируются.

    Подстановка идёт одним проходом по уже выбранному тексту, поэтому ``{{…}}`` внутри значения
    (например, в описании платежа) ничего не раскрывает.
    """

    def keep_blocks(text: str) -> str:
        def decide(match: re.Match[str]) -> str:
            return keep_blocks(match.group('body')) if match.group('name') in blocks else ''

        return _BLOCK_RE.sub(decide, text)

    escaped = {key: html.escape(value, quote=True) for key, value in values.items()}
    return _PLACEHOLDER_RE.sub(lambda match: escaped.get(match.group(1), match.group(0)), keep_blocks(template))


def _normalize_path(path: str) -> str:
    return '/' + path.strip().strip('/')


# ---------------------------------------------------------------- вспомогательное


def _json(content: dict[str, Any], status_code: int = status.HTTP_200_OK) -> JSONResponse:
    return JSONResponse(content, status_code=status_code, headers={'Cache-Control': 'no-store'})


def _error(code: str) -> JSONResponse:
    """Ответ по коду из ``AntarcticSessionError``: статус берётся оттуда же, что и у сессии."""
    return _json({'error': code}, AntarcticSessionError(code).http_status)


async def _read_body(request: Request, limit: int) -> bytes:
    """Читает тело не больше ``limit`` байт; больше — ``_BodyTooLarge`` без чтения остатка."""
    declared = request.headers.get('content-length')
    if declared is not None and declared.isascii() and declared.isdigit() and int(declared) > limit:
        raise _BodyTooLarge
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise _BodyTooLarge
        chunks.append(chunk)
    return b''.join(chunks)


def _parse_json(raw: bytes) -> Any:
    try:
        return json.loads(raw)
    except (ValueError, RecursionError):
        # ValueError — и невалидный JSON, и невалидный UTF-8; RecursionError — вложенность в десятки тысяч скобок.
        return None


def _credentials(payload: Any) -> tuple[str, str] | None:
    """``(idToken, token)`` из тела запроса или ``None``, если тело не годится."""
    if not isinstance(payload, dict):
        return None
    id_token = payload.get('idToken')
    token = payload.get('token')
    if not isinstance(id_token, str) or not 0 < len(id_token) <= MAX_ID_TOKEN_LENGTH:
        return None
    if not isinstance(token, str) or not _TOKEN_RE.fullmatch(token):
        return None
    return id_token, token


async def _fail_open(check: Callable[[], Awaitable[bool]], what: str) -> bool:
    """Результат лимитера; его сбой не должен останавливать оплату, если Redis недоступен."""
    try:
        return bool(await check())
    except Exception as error:
        logger.warning('Antarctic: лимитер недоступен, запрос пропущен', limiter=what, error=type(error).__name__)
        return False


async def _is_ip_rate_limited(request: Request) -> bool:
    try:
        client_ip = get_client_ip(request)
    except HTTPException:
        client_ip = 'unknown'
    return await _fail_open(
        lambda: RateLimitCache.is_rate_limited(
            0,
            f'antarctic:{client_ip}',
            limit=RATE_LIMIT_IP_REQUESTS,
            window=RATE_LIMIT_WINDOW_SECONDS,
        ),
        'ip',
    )


async def _is_token_rate_limited(token: str) -> bool:
    # Ключ лежит в Redis и в его логах, а токен платежа — это ссылка на оплату: кладём только отпечаток.
    digest = hashlib.sha256(token.encode('ascii')).hexdigest()
    return await _fail_open(
        lambda: RateLimitCache.is_subject_rate_limited(
            digest,
            'antarctic_token',
            limit=RATE_LIMIT_TOKEN_REQUESTS,
            window=RATE_LIMIT_WINDOW_SECONDS,
        ),
        'token',
    )


async def _read_credentials(request: Request) -> tuple[str, str] | JSONResponse:
    """Общее начало ``/session`` и ``/status``: лимиты, ограниченное чтение тела, разбор.

    Возвращает ``(idToken, token)`` либо готовый ответ с ошибкой. Порядок важен: лимит по IP стоит до
    чтения тела, лимит по токену — после разбора (мусорные тела и токены чужой формы в Redis не попадают),
    а проверка ``idToken`` — после обоих.
    """
    if await _is_ip_rate_limited(request):
        return _json({'error': 'rate_limited'}, status.HTTP_429_TOO_MANY_REQUESTS)
    try:
        raw = await _read_body(request, MAX_JSON_BODY_BYTES)
    except _BodyTooLarge:
        return _json({'error': 'payload_too_large'}, status.HTTP_413_CONTENT_TOO_LARGE)
    credentials = _credentials(_parse_json(raw))
    if credentials is None:
        return _json({'error': 'bad_request'}, status.HTTP_400_BAD_REQUEST)
    if await _is_token_rate_limited(credentials[1]):
        return _json({'error': 'rate_limited'}, status.HTTP_429_TOO_MANY_REQUESTS)
    return credentials


def _format_rub(amount_kopeks: int) -> str:
    rubles, kopeks = divmod(int(amount_kopeks), 100)
    text = f'{rubles:,}'.replace(',', '\u00a0')
    if kopeks:
        text = f'{text},{kopeks:02d}'
    return f'{text}\u00a0₽'


def _amount_rub_string(amount_kopeks: int) -> str:
    rubles, kopeks = divmod(int(amount_kopeks), 100)
    return f'{rubles}.{kopeks:02d}'


def _minutes_text(minutes: int) -> str:
    minutes = max(int(minutes), 1)
    if 11 <= minutes % 100 <= 14:
        word = 'минут'
    elif minutes % 10 == 1:
        word = 'минуту'
    elif 2 <= minutes % 10 <= 4:
        word = 'минуты'
    else:
        word = 'минут'
    return f'{minutes} {word}'


def _pay_page_state(payment: Any) -> str:
    """``paid`` / ``stale`` / ``pending``. Правила те же, что у сессии: TTL гасит только неоткрытый платёж."""
    if payment.is_paid:
        return 'paid'
    if payment.status in ANTARCTIC_TERMINAL_FAILURES:
        return 'stale'
    expires_at = payment.expires_at
    if not payment.operation_id and expires_at is not None:
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if datetime.now(UTC) > expires_at:
            return 'stale'
    return 'pending'


def _pay_page(status_code: int, blocks: set[str], values: dict[str, str] | None = None) -> HTMLResponse:
    headers = {
        **_PAGE_HEADERS,
        'Content-Security-Policy': PAY_PAGE_CSP,
        'X-Frame-Options': 'DENY',
        # Токен платежа сидит в адресе страницы, наружу он утекать не должен.
        'Referrer-Policy': 'no-referrer',
        'X-Robots-Tag': 'noindex, nofollow',
    }
    content = _render(_template('pay.html'), values or {}, frozenset(blocks))
    return HTMLResponse(content, status_code=status_code, headers=headers)


# ---------------------------------------------------------------- роутер


def create_antarctic_router(payment_service: PaymentService) -> APIRouter:
    """Страницы, JSON-эндпоинты и вебхук Antarctic Wallet; пути берутся из настроек на момент монтирования."""
    router = APIRouter()

    if settings.is_antarctic_configured() and not settings.get_cabinet_trusted_proxies():
        # Без списка доверенных прокси get_client_ip отдаёт адрес самого прокси: лимит по IP
        # тогда общий на всех пользователей.
        logger.warning('Antarctic: rate limits are per proxy IP until CABINET_TRUSTED_PROXIES is set')

    miniapp_path = _normalize_path(settings.ANTARCTIC_MINIAPP_PATH)
    pay_page_path = _normalize_path(settings.ANTARCTIC_PAY_PAGE_PATH)
    webhook_path = _normalize_path(settings.ANTARCTIC_WEBHOOK_PATH)

    @router.get(miniapp_path, include_in_schema=False)
    async def antarctic_miniapp() -> HTMLResponse:
        content = _render(
            _template('app.html'),
            {'APP_ID': settings.ANTARCTIC_APP_ID or '', 'BASE': miniapp_path},
        )
        return HTMLResponse(content, headers={**_PAGE_HEADERS, 'Content-Security-Policy': MINIAPP_CSP})

    @router.post(f'{miniapp_path}/session', include_in_schema=False)
    async def antarctic_session(request: Request) -> JSONResponse:
        credentials = await _read_credentials(request)
        if isinstance(credentials, JSONResponse):
            return credentials
        id_token, token = credentials

        try:
            async with AsyncSessionLocal() as db:
                result = await payment_service.start_antarctic_session(db, token=token, id_token=id_token)
        except AntarcticSessionError as error:
            return _json({'error': error.code}, error.http_status)
        except Exception as error:
            # Только имя класса: в тексте исключения может оказаться что угодно из запроса.
            logger.error('Antarctic: сбой сессии мини-приложения', error=type(error).__name__)
            return _json({'error': 'server_error'}, status.HTTP_500_INTERNAL_SERVER_ERROR)

        return _json(
            {
                'operationId': result['operation_id'],
                'amountUsd': result['amount_usd'],
                'amountRub': _amount_rub_string(result['amount_kopeks']),
                'status': result['status'],
                'isPaid': bool(result['is_paid']),
            }
        )

    @router.post(f'{miniapp_path}/status', include_in_schema=False)
    async def antarctic_status(request: Request) -> JSONResponse:
        credentials = await _read_credentials(request)
        if isinstance(credentials, JSONResponse):
            return credentials
        id_token, token = credentials

        client = get_antarctic_client()
        if client is None:
            return _error('provider_error')
        try:
            login = await client.verify_id_token(id_token)
        except AntarcticTokenError:
            return _error('invalid_token')
        except Exception as error:
            logger.error('Antarctic: сбой проверки idToken', error=type(error).__name__)
            return _json({'error': 'server_error'}, status.HTTP_500_INTERNAL_SERVER_ERROR)

        try:
            async with AsyncSessionLocal() as db:
                payment = await antarctic_crud.get_antarctic_payment_by_token(db, token)
                if payment is None:
                    return _error('not_found')
                # Статус отдаётся только тому кошельку, который открыл платёж.
                if not payment.user_app_login or payment.user_app_login != login:
                    return _error('login_mismatch')
                result = await payment_service.refresh_antarctic_payment(db, payment.id)
        except Exception as error:
            logger.error('Antarctic: сбой проверки статуса', error=type(error).__name__)
            return _json({'error': 'server_error'}, status.HTTP_500_INTERNAL_SERVER_ERROR)

        if result is None:
            return _error('not_found')
        return _json({'status': result['status'], 'isPaid': bool(result['is_paid'])})

    @router.get(f'{miniapp_path}/{{asset_name}}', include_in_schema=False)
    async def antarctic_sdk_asset(asset_name: str) -> Response:
        content = _sdk_assets().get(asset_name)
        if content is None:
            return Response(status_code=status.HTTP_404_NOT_FOUND)
        return Response(
            content,
            media_type='application/javascript',
            headers={'Cache-Control': _SDK_CACHE_CONTROL, 'X-Content-Type-Options': 'nosniff'},
        )

    @router.get(f'{pay_page_path}/{{token}}', include_in_schema=False)
    async def antarctic_pay_page(token: str) -> HTMLResponse:
        if not _TOKEN_RE.fullmatch(token):
            return _pay_page(status.HTTP_404_NOT_FOUND, {'notfound'})

        async with AsyncSessionLocal() as db:
            payment = await antarctic_crud.get_antarctic_payment_by_token(db, token)
        if payment is None:
            return _pay_page(status.HTTP_404_NOT_FOUND, {'notfound'})

        state = _pay_page_state(payment)
        if state == 'paid':
            return _pay_page(status.HTTP_200_OK, {'paid'})
        if state == 'stale':
            return _pay_page(status.HTTP_410_GONE, {'stale'})

        description = (payment.description or '').strip()[:_MAX_DESCRIPTION_LENGTH]
        referral_url = settings.get_antarctic_referral_url()
        blocks = {'pending'}
        if description:
            blocks.add('description')
        if referral_url:
            blocks.add('referral')
        return _pay_page(
            status.HTTP_200_OK,
            blocks,
            {
                'AMOUNT_USD': str(payment.amount_usd),
                'AMOUNT_RUB': _format_rub(payment.amount_kopeks),
                'DESCRIPTION': description,
                'APP_LINK': settings.get_antarctic_app_link(token),
                'REFERRAL_URL': referral_url or '',
                'TTL_TEXT': _minutes_text(settings.ANTARCTIC_PAYMENT_TTL_MINUTES),
            },
        )

    @router.get(webhook_path, include_in_schema=False)
    async def antarctic_webhook_health() -> JSONResponse:
        return JSONResponse(
            {
                'status': 'ok',
                'service': 'antarctic_webhook',
                'enabled': settings.is_antarctic_enabled(),
            }
        )

    @router.post(webhook_path, include_in_schema=False)
    async def antarctic_webhook(request: Request) -> JSONResponse:
        try:
            raw_body = await _read_body(request, MAX_WEBHOOK_BODY_BYTES)
        except _BodyTooLarge:
            return JSONResponse({'status': 'error'}, status_code=status.HTTP_413_CONTENT_TOO_LARGE)

        client = get_antarctic_client()
        if client is None:
            return JSONResponse({'status': 'error'}, status_code=status.HTTP_503_SERVICE_UNAVAILABLE)

        # Подпись считается по сырым байтам, поэтому проверяем её до разбора JSON. Значения заголовков в лог не идут.
        if not client.verify_webhook(
            raw_body,
            request.headers.get('X-Sdk-Webhook-Timestamp'),
            request.headers.get('X-Sdk-Webhook-Signature'),
        ):
            logger.warning('Antarctic webhook: подпись не принята')
            return JSONResponse({'status': 'unauthorized'}, status_code=status.HTTP_401_UNAUTHORIZED)

        payload = _parse_json(raw_body)
        if not isinstance(payload, dict):
            logger.error('Antarctic webhook: тело не JSON-объект')
            return JSONResponse({'status': 'error'}, status_code=status.HTTP_400_BAD_REQUEST)

        try:
            success = await payments_module._process_payment_service_callback(
                payment_service,
                payload,
                'process_antarctic_webhook',
            )
        except Exception as error:
            logger.error('Antarctic webhook: ошибка обработки', error=type(error).__name__)
            success = False

        if not success:
            event = payload.get('event')
            logger.error(
                'Antarctic webhook: обработка не удалась',
                webhook_event=event[:64] if isinstance(event, str) else None,
            )
            # 5xx — AW повторит доставку; для сбоев, которые повтор может вылечить, нужен именно он.
            return JSONResponse({'status': 'error'}, status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

        return JSONResponse({'status': 'ok'}, status_code=status.HTTP_200_OK)

    return router
