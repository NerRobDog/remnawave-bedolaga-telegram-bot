"""Варианты анкеты из конфига страницы подписки Remnawave: платформы, приложения, запасной набор, кэш."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.services.support_assist import form_options
from app.services.support_assist.form_options import (
    FALLBACK_APPS,
    LEGACY_APP,
    LOCATIONS,
    build_form_options,
    get_form_options,
)


def _app(name: str) -> dict:
    return {'name': name, 'urlScheme': 'x://', 'blocks': []}


FAKE_CONFIG = {
    '_isRemnawave': True,
    'platforms': {
        'ios': {'displayName': {'ru': 'iOS', 'en': 'iOS'}, 'apps': [_app('Clash Mi'), _app('RabbitHole')]},
        'android': {
            'displayName': {'ru': 'Андроид', 'en': 'Android'},
            'apps': [_app('FlClashX'), _app('Clash Mi'), _app('FlClashX')],
        },
        'appleTV': {'displayName': {'ru': 'Apple TV', 'en': 'Apple TV'}, 'apps': [_app('Happ')]},
    },
}


@pytest.fixture(autouse=True)
def _fresh_cache():
    form_options.reset_cache()
    yield
    form_options.reset_cache()


def test_platforms_keep_config_order_then_router_and_other():
    opts = build_form_options(FAKE_CONFIG)
    assert opts.to_api()['platforms'] == [
        {'id': 'ios', 'name': {'ru': 'iOS', 'en': 'iOS'}},
        {'id': 'android', 'name': {'ru': 'Андроид', 'en': 'Android'}},
        {'id': 'appleTV', 'name': {'ru': 'Apple TV', 'en': 'Apple TV'}},
        {'id': 'router', 'name': None},
        {'id': 'other', 'name': None},
    ]
    assert opts.from_config is True


def test_apps_per_platform_dedup_keep_order_and_append_happ():
    apps = build_form_options(FAKE_CONFIG).to_api()['apps']
    assert apps['ios'] == ['Clash Mi', 'RabbitHole', 'Happ']
    assert apps['android'] == ['FlClashX', 'Clash Mi', 'Happ']
    assert apps['appleTV'] == ['Happ']  # уже есть: не дублируем


def test_router_and_other_get_the_union_of_all_apps_plus_happ():
    apps = build_form_options(FAKE_CONFIG).to_api()['apps']
    union = ['Clash Mi', 'RabbitHole', 'FlClashX', 'Happ']
    assert apps['router'] == union
    assert apps['other'] == union
    assert 'other' not in union  # «Другое» добавляет фронтенд


def test_locations_are_static():
    assert build_form_options(FAKE_CONFIG).to_api()['locations'] == ['russia', 'abroad']
    assert LOCATIONS == ('russia', 'abroad')
    assert LEGACY_APP == 'Happ'


def test_bad_entries_in_the_config_are_skipped():
    config = {
        'platforms': {
            'ios': {'displayName': {'ru': 'iOS', 'en': 'iOS'}, 'apps': [_app('Clash Mi'), 'junk', {'name': ''}]},
            'bad key!': {'displayName': {'en': 'x'}, 'apps': [_app('Evil')]},
            'router': {'displayName': {'en': 'Dup'}, 'apps': [_app('RouterApp')]},
            'linux': 'not a dict',
            'macos': {'apps': [_app('A' * 41), _app('Line\nBreak'), _app('Ok')]},
        }
    }
    api = build_form_options(config).to_api()
    assert [p['id'] for p in api['platforms']] == ['ios', 'macos', 'router', 'other']
    assert api['apps']['ios'] == ['Clash Mi', 'Happ']
    assert api['apps']['macos'] == ['Ok', 'Happ']
    assert api['platforms'][1]['name'] == {'ru': 'macOS', 'en': 'macOS'}  # нет displayName: статичное имя


@pytest.mark.parametrize('config', [None, {}, {'platforms': {}}, {'platforms': []}, 'x'])
def test_unusable_config_gives_the_fallback(config):
    opts = build_form_options(config)
    api = opts.to_api()
    assert opts.from_config is False
    assert [p['id'] for p in api['platforms']] == ['ios', 'android', 'windows', 'macos', 'router', 'other']
    assert api['platforms'][0]['name'] == {'ru': 'iOS', 'en': 'iOS'}
    assert api['platforms'][-1]['name'] is None
    assert all(apps == list(FALLBACK_APPS) for apps in api['apps'].values())
    assert list(FALLBACK_APPS) == ['Clash Mi', 'Happ', 'RabbitHole', 'FlClashX']


@pytest.mark.asyncio
async def test_loader_exception_gives_the_fallback(monkeypatch):
    monkeypatch.setattr(form_options, '_load_config', AsyncMock(side_effect=RuntimeError('remnawave down')))
    opts = await get_form_options()
    assert opts.from_config is False


@pytest.mark.asyncio
async def test_options_are_cached_for_five_minutes(monkeypatch):
    loader = AsyncMock(return_value=FAKE_CONFIG)
    monkeypatch.setattr(form_options, '_load_config', loader)
    clock = [1000.0]
    monkeypatch.setattr(form_options, '_now', lambda: clock[0])

    first = await get_form_options()
    clock[0] += 299
    assert await get_form_options() is first
    assert loader.await_count == 1

    clock[0] += 2  # 301 с с первой загрузки
    await get_form_options()
    assert loader.await_count == 2


@pytest.mark.asyncio
async def test_fallback_is_also_cached(monkeypatch):
    """Лежащий Remnawave не дёргаем на каждое открытие страницы поддержки."""
    loader = AsyncMock(return_value=None)
    monkeypatch.setattr(form_options, '_load_config', loader)
    await get_form_options()
    await get_form_options()
    assert loader.await_count == 1


def test_lookup_helpers():
    opts = build_form_options(FAKE_CONFIG)
    assert opts.platform_ids() == ('ios', 'android', 'appleTV', 'router', 'other')
    assert opts.all_apps() == ('Clash Mi', 'RabbitHole', 'FlClashX', 'Happ')
    assert opts.apps_for('ios') == ('Clash Mi', 'RabbitHole', 'Happ')
    assert opts.apps_for('nope') == opts.all_apps()
    assert opts.platform_name('android', 'ru') == 'Андроид'
    assert opts.platform_name('android', 'zh') == 'Android'  # нет языка: английское
    assert opts.platform_name('router', 'ru') is None
