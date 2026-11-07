import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import settings
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


@pytest.mark.asyncio
async def test_effort_comes_from_settings(monkeypatch):
    assert settings.SUPPORT_ASSIST_EFFORT == 'medium'
    monkeypatch.setattr(settings, 'SUPPORT_ASSIST_EFFORT', 'high')
    client = _client(_response(json.dumps(VALID)))
    await generate_card(client, _payload(), ['x'])
    assert client.beta.messages.create.call_args.kwargs['output_config']['effort'] == 'high'


@pytest.mark.asyncio
async def test_unknown_effort_falls_back_to_medium(monkeypatch):
    monkeypatch.setattr(settings, 'SUPPORT_ASSIST_EFFORT', 'turbo')
    client = _client(_response(json.dumps(VALID)))
    await generate_card(client, _payload(), ['x'])
    assert client.beta.messages.create.call_args.kwargs['output_config']['effort'] == 'medium'


@pytest.mark.asyncio
async def test_fallbacks_can_be_switched_off(monkeypatch):
    assert settings.SUPPORT_ASSIST_FALLBACKS_ENABLED is True
    monkeypatch.setattr(settings, 'SUPPORT_ASSIST_FALLBACKS_ENABLED', False)
    client = _client(_response(json.dumps(VALID)))
    await generate_card(client, _payload(), ['x'])
    kwargs = client.beta.messages.create.call_args.kwargs
    assert 'fallbacks' not in kwargs
    assert 'betas' not in kwargs


@pytest.mark.asyncio
async def test_cost_is_priced_by_the_model_that_answered():
    """Ответил запасной Sonnet 5.5 ($2/$10), а не настроенный Opus 5.5: цена по ответившей модели."""
    client = _client(_response(json.dumps(VALID), model='claude-sonnet-5-5'))
    result = await generate_card(client, _payload(), ['x'])
    # 1000*2 + 500*10 + 2000*0.2 per 1M
    assert float(result.cost_usd) == pytest.approx(0.0074)
    assert result.model == 'claude-sonnet-5-5'
    assert result.cost_is_estimate is False


@pytest.mark.asyncio
async def test_cost_of_the_configured_model_is_not_an_estimate():
    result = await generate_card(_client(_response(json.dumps(VALID))), _payload(), ['x'])
    assert result.cost_is_estimate is False


@pytest.mark.asyncio
async def test_cost_for_an_unpriced_model_is_marked_as_estimate(caplog):
    client = _client(_response(json.dumps(VALID), model='claude-future-9'))
    result = await generate_card(client, _payload(), ['x'])
    assert result.cost_is_estimate is True
    assert result.model == 'claude-future-9'
    # цена настроенной модели как оценка
    assert float(result.cost_usd) == pytest.approx(0.0144)


@pytest.mark.asyncio
async def test_unknown_response_model_means_configured_model_pricing_without_estimate_flag():
    client = _client(_response(json.dumps(VALID), model=None))
    result = await generate_card(client, _payload(), ['x'])
    assert float(result.cost_usd) == pytest.approx(0.0144)
    assert result.cost_is_estimate is False


def test_prompt_marks_diagnostics_as_unverified_client_answers():
    """Анкета — слова клиента: в промпте она слабее измеренных проверок."""
    from app.services.support_assist.card import _system_blocks

    prompt = _system_blocks()[0]['text']
    line = next(row for row in prompt.splitlines() if 'diagnostics' in row)
    assert 'не проверенные' in line and 'слабее измеренных' in line


def test_schema_is_strict_objects_without_unsupported_keywords():
    from app.services.support_assist.card import _card_schema

    dumped = json.dumps(_card_schema())
    assert 'maxItems' not in dumped and 'minItems' not in dumped
    assert _card_schema()['additionalProperties'] is False
