"""Карточка разбора от Claude: структурный ответ, цена вызова, отказоустойчивость.

Любая ошибка (сеть, отказ модели, невалидный JSON) превращается в ``CardResult(card=None, error=...)``;
вызывающий код рисует карточку из флагов без модели.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, NamedTuple

import anthropic
import structlog
from pydantic import BaseModel, field_validator

from app.config import settings


logger = structlog.get_logger(__name__)

_HERE = Path(__file__).parent
MAX_FACTS = 7
FALLBACKS_BETA = 'server-side-fallback-2026-07-01'

_MILLION = Decimal(1_000_000)


class ModelPrice(NamedTuple):
    """$ за 1M токенов: вход, выход, чтение кэша, запись кэша (кэш на 5 минут)."""

    input: Decimal
    output: Decimal
    cache_read: Decimal
    cache_write: Decimal


# Цены из таблицы моделей скилла claude-api (срез 2026-10-06). Чтение кэша у Opus 5.5 — $0.20, у Fable 5.1 — $0.25;
# у остальных 0.1 от входа, запись 1.25 от входа (общее правило из документации по кэшу).
PRICES: dict[str, ModelPrice] = {
    'claude-opus-5-5': ModelPrice(Decimal(4), Decimal(20), Decimal('0.20'), Decimal(5)),
    'claude-opus-5': ModelPrice(Decimal(5), Decimal(25), Decimal('0.50'), Decimal('6.25')),
    'claude-opus-4-8': ModelPrice(Decimal(5), Decimal(25), Decimal('0.50'), Decimal('6.25')),
    'claude-sonnet-5-5': ModelPrice(Decimal(2), Decimal(10), Decimal('0.20'), Decimal('2.50')),
    'claude-fable-5-1': ModelPrice(Decimal(10), Decimal(50), Decimal('0.25'), Decimal('12.50')),
}
# Модель, цену которой берём, когда настроенной нет в таблице.
DEFAULT_PRICE_MODEL = 'claude-opus-5-5'

EFFORT_LEVELS = frozenset({'low', 'medium', 'high', 'xhigh', 'max'})
DEFAULT_EFFORT = 'medium'

MAX_OUTPUT_TOKENS = 16000


class CardFact(BaseModel):
    icon: Literal['✅', 'ℹ️', '⚠️', '🔴', '🚨', '❓']
    text: str


class Card(BaseModel):
    facts: list[CardFact]
    cause: str
    owner_action: str
    ask_client: list[str]
    draft: str

    @field_validator('facts', mode='after')
    @classmethod
    def _cap_facts(cls, value: list[CardFact]) -> list[CardFact]:
        # Схема структурного вывода не держит maxItems, поэтому лишнее режем здесь.
        return value[:MAX_FACTS]


@dataclass
class CardResult:
    card: Card | None
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cost_usd: Decimal | None = None
    # Цена посчитана по тарифу настроенной модели, а ответила другая, которой нет в таблице: порядок величины, не счёт.
    cost_is_estimate: bool = False
    error: str | None = None


def _card_schema() -> dict[str, Any]:
    schema = Card.model_json_schema()

    def fix(node: Any) -> None:
        if isinstance(node, dict):
            if node.get('type') == 'object':
                node['additionalProperties'] = False
            for key in ('title', 'default', 'maxItems', 'minItems'):
                node.pop(key, None)
            for value in node.values():
                fix(value)
        elif isinstance(node, list):
            for item in node:
                fix(item)

    fix(schema)
    return schema


def _system_blocks() -> list[dict[str, Any]]:
    prompt = (_HERE / 'prompt.md').read_text(encoding='utf-8')
    kb = (_HERE / 'kb.md').read_text(encoding='utf-8')
    return [
        {'type': 'text', 'text': prompt},
        {
            'type': 'text',
            'text': 'База знаний сервиса:\n' + kb,
            'cache_control': {'type': 'ephemeral'},
        },
    ]


def _prices_for(model: str | None) -> tuple[ModelPrice, bool]:
    """Тариф ответившей модели; нет в таблице — тариф настроенной, и если ответила не она, это оценка."""
    if model in PRICES:
        return PRICES[model], False
    configured = PRICES.get(settings.SUPPORT_ASSIST_MODEL)
    estimate = configured is None or (model is not None and model != settings.SUPPORT_ASSIST_MODEL)
    return configured or PRICES[DEFAULT_PRICE_MODEL], estimate


def _cost(price: ModelPrice, input_tokens: int, output_tokens: int, cache_read: int, cache_write: int) -> Decimal:
    total = (
        price.input * input_tokens
        + price.output * output_tokens
        + price.cache_read * cache_read
        + price.cache_write * cache_write
    )
    return total / _MILLION


def _effort() -> str:
    effort = str(settings.SUPPORT_ASSIST_EFFORT or '').strip().lower()
    if effort in EFFORT_LEVELS:
        return effort
    logger.warning('support_assist.unknown_effort', effort=effort, used=DEFAULT_EFFORT)
    return DEFAULT_EFFORT


async def generate_card(
    client: anthropic.AsyncAnthropic,
    payload: dict,
    client_messages: list[str],
) -> CardResult:
    """Запросить карточку у Claude. Не бросает исключений."""
    try:
        messages_text = '\n\n'.join(f'Сообщение клиента:\n{m}' for m in client_messages) or 'Сообщений нет.'
        user_content = [
            {'type': 'text', 'text': messages_text},
            {
                'type': 'text',
                'text': 'Результат автопроверок (JSON):\n' + json.dumps(payload, ensure_ascii=False),
            },
        ]
        request: dict[str, Any] = {
            'model': settings.SUPPORT_ASSIST_MODEL,
            'max_tokens': MAX_OUTPUT_TOKENS,
            'thinking': {'type': 'adaptive'},
            'output_config': {
                'effort': _effort(),
                'format': {'type': 'json_schema', 'schema': _card_schema()},
            },
            'system': _system_blocks(),
            'messages': [{'role': 'user', 'content': user_content}],
            'timeout': settings.SUPPORT_ASSIST_TIMEOUT_SECONDS,
        }
        if settings.SUPPORT_ASSIST_FALLBACKS_ENABLED:
            request['betas'] = [FALLBACKS_BETA]
            request['fallbacks'] = 'default'
        response = await client.beta.messages.create(**request)
    except Exception as exc:
        logger.warning('support_assist.card_request_failed', error=str(exc))
        return CardResult(card=None, error=f'{type(exc).__name__}: {exc}')

    usage = response.usage
    in_tok = int(getattr(usage, 'input_tokens', 0) or 0)
    out_tok = int(getattr(usage, 'output_tokens', 0) or 0)
    cache_read = int(getattr(usage, 'cache_read_input_tokens', 0) or 0)
    cache_write = int(getattr(usage, 'cache_creation_input_tokens', 0) or 0)
    answered_by = getattr(response, 'model', None)
    price, estimate = _prices_for(answered_by)
    if estimate:
        logger.warning(
            'support_assist.cost_estimated',
            answered_by=answered_by,
            configured=settings.SUPPORT_ASSIST_MODEL,
        )
    result = CardResult(
        card=None,
        model=answered_by,
        input_tokens=in_tok,
        output_tokens=out_tok,
        cache_read_tokens=cache_read,
        cost_usd=_cost(price, in_tok, out_tok, cache_read, cache_write),
        cost_is_estimate=estimate,
    )
    if response.stop_reason == 'refusal':
        result.error = 'refusal'
        return result
    try:
        text = ''.join(b.text for b in response.content if b.type == 'text')
        result.card = Card.model_validate(json.loads(text))
    except Exception as exc:
        logger.warning('support_assist.card_parse_failed', error=str(exc))
        result.error = f'invalid_card: {type(exc).__name__}: {exc}'
    return result
