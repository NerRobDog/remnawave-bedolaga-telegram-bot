"""Карточка разбора от Claude: структурный ответ, цена вызова, отказоустойчивость.

Любая ошибка (сеть, отказ модели, невалидный JSON) превращается в ``CardResult(card=None, error=...)``;
вызывающий код рисует карточку из флагов без модели.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import anthropic
import structlog
from pydantic import BaseModel, field_validator

from app.config import settings


logger = structlog.get_logger(__name__)

_HERE = Path(__file__).parent
MAX_FACTS = 7
FALLBACKS_BETA = 'server-side-fallback-2026-07-01'

# Цены Opus 5.5, $ за 1M токенов.
PRICE_INPUT = Decimal(4)
PRICE_OUTPUT = Decimal(20)
PRICE_CACHE_READ = Decimal('0.20')
PRICE_CACHE_WRITE = Decimal(5)  # 1.25x от входа (кэш на 5 минут)
_MILLION = Decimal(1_000_000)

MAX_OUTPUT_TOKENS = 16000


class CardFact(BaseModel):
    icon: Literal['✅', '⚠️', '🔴', '🚨', '❓']
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


def _cost(input_tokens: int, output_tokens: int, cache_read: int, cache_write: int) -> Decimal:
    total = (
        PRICE_INPUT * input_tokens
        + PRICE_OUTPUT * output_tokens
        + PRICE_CACHE_READ * cache_read
        + PRICE_CACHE_WRITE * cache_write
    )
    return total / _MILLION


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
        response = await client.beta.messages.create(
            model=settings.SUPPORT_ASSIST_MODEL,
            max_tokens=MAX_OUTPUT_TOKENS,
            thinking={'type': 'adaptive'},
            output_config={
                'effort': 'medium',
                'format': {'type': 'json_schema', 'schema': _card_schema()},
            },
            system=_system_blocks(),
            messages=[{'role': 'user', 'content': user_content}],
            betas=[FALLBACKS_BETA],
            fallbacks='default',
            timeout=settings.SUPPORT_ASSIST_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        logger.warning('support_assist.card_request_failed', error=str(exc))
        return CardResult(card=None, error=f'{type(exc).__name__}: {exc}')

    usage = response.usage
    in_tok = int(getattr(usage, 'input_tokens', 0) or 0)
    out_tok = int(getattr(usage, 'output_tokens', 0) or 0)
    cache_read = int(getattr(usage, 'cache_read_input_tokens', 0) or 0)
    cache_write = int(getattr(usage, 'cache_creation_input_tokens', 0) or 0)
    result = CardResult(
        card=None,
        model=getattr(response, 'model', None),
        input_tokens=in_tok,
        output_tokens=out_tok,
        cache_read_tokens=cache_read,
        cost_usd=_cost(in_tok, out_tok, cache_read, cache_write),
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
