import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.support_assist.card import Card, CardResult, generate_card
from app.services.support_assist.flags import compute_flags
from app.services.support_assist.payload import build_payload, find_forbidden_keys
from tests.services.support_assist.fixtures import healthy, leaky_snapshot


VALID = {
    'facts': [{'icon': '🔴', 'text': 'Подписку приложение ни разу не забирало'}],
    'cause': 'Ссылка не добавлена в приложение',
    'owner_action': 'нет',
    'ask_client': ['Какое приложение?'],
    'draft': 'Добавьте ссылку подписки в приложение.',
}


def _response(text, stop_reason='end_turn', model='claude-opus-5-5'):
    usage = SimpleNamespace(
        input_tokens=1000, output_tokens=500, cache_read_input_tokens=2000, cache_creation_input_tokens=0
    )
    return SimpleNamespace(
        content=[SimpleNamespace(type='text', text=text)], usage=usage, stop_reason=stop_reason, model=model
    )


def _client(response=None, exc=None):
    client = MagicMock()
    client.beta.messages.create = AsyncMock(return_value=response, side_effect=exc)
    return client


def _payload():
    s = healthy()
    return build_payload(s, compute_flags(s))


@pytest.mark.asyncio
async def test_valid_json_gives_card_and_cost():
    client = _client(_response(json.dumps(VALID)))
    result = await generate_card(client, _payload(), ['не работает'])
    assert isinstance(result, CardResult)
    assert result.error is None
    assert result.card is not None and result.card.cause == VALID['cause']
    assert result.model == 'claude-opus-5-5'
    assert (result.input_tokens, result.output_tokens, result.cache_read_tokens) == (1000, 500, 2000)
    # 1000*4 + 500*20 + 2000*0.2 per 1M
    assert result.cost_usd is not None and float(result.cost_usd) == pytest.approx(0.0144)


@pytest.mark.asyncio
async def test_api_error_gives_no_card():
    client = _client(exc=RuntimeError('boom'))
    result = await generate_card(client, _payload(), ['x'])
    assert result.card is None
    assert 'boom' in (result.error or '')


@pytest.mark.asyncio
async def test_invalid_json_gives_no_card():
    result = await generate_card(_client(_response('not json')), _payload(), ['x'])
    assert result.card is None and result.error


@pytest.mark.asyncio
async def test_schema_violation_gives_no_card():
    bad = dict(VALID, facts=[{'icon': 'zzz', 'text': 'x'}])
    result = await generate_card(_client(_response(json.dumps(bad))), _payload(), ['x'])
    assert result.card is None and result.error


@pytest.mark.asyncio
async def test_refusal_gives_no_card():
    result = await generate_card(_client(_response('', stop_reason='refusal')), _payload(), ['x'])
    assert result.card is None and 'refusal' in (result.error or '')


def test_card_truncates_facts_to_seven():
    facts = [{'icon': '✅', 'text': str(i)} for i in range(12)]
    card = Card.model_validate(dict(VALID, facts=facts))
    assert len(card.facts) == 7


@pytest.mark.asyncio
async def test_request_kwargs():
    client = _client(_response(json.dumps(VALID)))
    s = leaky_snapshot()
    payload = build_payload(s, compute_flags(s))
    await generate_card(client, payload, ['сообщение клиента'])
    kwargs = client.beta.messages.create.call_args.kwargs
    assert kwargs['model'] == 'claude-opus-5-5'
    assert kwargs['thinking']['type'] == 'adaptive'
    assert kwargs['fallbacks'] == 'default'
    assert 'server-side-fallback-2026-07-01' in kwargs['betas']
    assert kwargs['output_config']['format']['type'] == 'json_schema'
    system = kwargs['system']
    assert len(system) == 2
    assert 'cache_control' not in system[0]
    assert system[1]['cache_control'] == {'type': 'ephemeral'}
    blocks = kwargs['messages'][0]['content']
    assert 'сообщение клиента' in blocks[0]['text']
    body = blocks[-1]['text']
    data = json.loads(body[body.index('{') :])
    assert find_forbidden_keys(data) == []
    assert 'LEAK' not in body


def test_schema_is_strict_objects_without_unsupported_keywords():
    from app.services.support_assist.card import _card_schema

    dumped = json.dumps(_card_schema())
    assert 'maxItems' not in dumped and 'minItems' not in dumped
    assert _card_schema()['additionalProperties'] is False
