import pytest

from app.config import settings


TELEGRAM_CONTACT = '@help'
EXTERNAL_CONTACT = 'https://help.example.com'


async def _get_config(monkeypatch, mode: str, contact: str):
    from app.cabinet.routes.info import get_support_config

    monkeypatch.setattr(settings, 'SUPPORT_SYSTEM_MODE', mode, raising=False)
    monkeypatch.setattr(settings, 'SUPPORT_USERNAME', contact, raising=False)

    return await get_support_config()


@pytest.mark.asyncio
async def test_contact_mode_with_telegram_username(monkeypatch):
    response = await _get_config(monkeypatch, 'contact', TELEGRAM_CONTACT)

    assert response.tickets_enabled is False
    assert response.support_type == 'profile'
    assert response.support_url == 'https://t.me/help'
    assert response.support_username == '@help'
    assert response.contact_is_telegram is True


@pytest.mark.asyncio
async def test_contact_mode_with_external_url(monkeypatch):
    response = await _get_config(monkeypatch, 'contact', EXTERNAL_CONTACT)

    assert response.tickets_enabled is False
    # Раньше здесь всегда был "profile", и клиент склеивал t.me/https://help...
    assert response.support_type == 'url'
    assert response.support_url == EXTERNAL_CONTACT
    assert response.contact_is_telegram is False


@pytest.mark.asyncio
async def test_both_mode_exposes_external_url(monkeypatch):
    response = await _get_config(monkeypatch, 'both', EXTERNAL_CONTACT)

    assert response.tickets_enabled is True
    assert response.support_type == 'both'
    assert response.support_url == EXTERNAL_CONTACT
    assert response.contact_is_telegram is False


@pytest.mark.asyncio
async def test_both_mode_with_telegram_username(monkeypatch):
    response = await _get_config(monkeypatch, 'both', TELEGRAM_CONTACT)

    assert response.tickets_enabled is True
    assert response.support_type == 'both'
    assert response.support_url == 'https://t.me/help'
    assert response.contact_is_telegram is True


@pytest.mark.asyncio
@pytest.mark.parametrize('contact', [TELEGRAM_CONTACT, EXTERNAL_CONTACT])
async def test_tickets_mode_ignores_contact(monkeypatch, contact):
    response = await _get_config(monkeypatch, 'tickets', contact)

    assert response.tickets_enabled is True
    assert response.support_type == 'tickets'


@pytest.mark.asyncio
async def test_empty_contact_yields_no_url(monkeypatch):
    response = await _get_config(monkeypatch, 'both', '')

    assert response.support_url is None
    assert response.support_username is None
    assert response.contact_is_telegram is False


@pytest.mark.asyncio
@pytest.mark.parametrize('flag', [False, True])
async def test_support_config_exposes_ticket_form_requirement(monkeypatch, flag):
    """Кабинет узнаёт из конфига, нужна ли анкета: включение флага не должно ломать создание тикета 422-й."""
    monkeypatch.setattr(settings, 'SUPPORT_TICKET_FORM_REQUIRED', flag)

    response = await _get_config(monkeypatch, 'both', TELEGRAM_CONTACT)

    assert response.ticket_form_required is flag
    assert response.model_dump()['ticket_form_required'] is flag


@pytest.mark.asyncio
async def test_ticket_form_requirement_defaults_to_off(monkeypatch):
    from app.cabinet.routes.info import SupportConfigResponse

    assert SupportConfigResponse(tickets_enabled=True, support_type='tickets').ticket_form_required is False


@pytest.fixture
def _options(monkeypatch):
    from unittest.mock import AsyncMock

    from app.services.support_assist import form_options

    form_options.reset_cache()
    config = {
        'platforms': {
            'ios': {'displayName': {'ru': 'iOS', 'en': 'iOS'}, 'apps': [{'name': 'Clash Mi'}]},
            'appleTV': {'displayName': {'ru': 'Apple TV', 'en': 'Apple TV'}, 'apps': [{'name': 'Happ'}]},
        }
    }
    loader = AsyncMock(return_value=config)
    monkeypatch.setattr(form_options, '_load_config', loader)
    yield loader
    form_options.reset_cache()


@pytest.mark.asyncio
@pytest.mark.parametrize('flag', [False, True])
async def test_support_config_exposes_ticket_form_options(monkeypatch, _options, flag):
    monkeypatch.setattr(settings, 'SUPPORT_TICKET_FORM_REQUIRED', flag)

    response = await _get_config(monkeypatch, 'both', TELEGRAM_CONTACT)

    options = response.model_dump()['ticket_form_options']
    assert options == {
        'platforms': [
            {'id': 'ios', 'name': {'ru': 'iOS', 'en': 'iOS'}},
            {'id': 'appleTV', 'name': {'ru': 'Apple TV', 'en': 'Apple TV'}},
            {'id': 'router', 'name': None},
            {'id': 'other', 'name': None},
        ],
        'apps': {
            'ios': ['Clash Mi', 'Happ'],
            'appleTV': ['Happ'],
            'router': ['Clash Mi', 'Happ'],
            'other': ['Clash Mi', 'Happ'],
        },
        'locations': ['russia', 'abroad'],
    }


@pytest.mark.asyncio
async def test_support_config_survives_a_remnawave_failure(monkeypatch, _options):
    _options.side_effect = RuntimeError('remnawave down')

    response = await _get_config(monkeypatch, 'both', TELEGRAM_CONTACT)

    platforms = [p['id'] for p in response.model_dump()['ticket_form_options']['platforms']]
    assert platforms == ['ios', 'android', 'windows', 'macos', 'router', 'other']


@pytest.mark.asyncio
async def test_support_config_reuses_cached_options(monkeypatch, _options):
    await _get_config(monkeypatch, 'both', TELEGRAM_CONTACT)
    await _get_config(monkeypatch, 'both', TELEGRAM_CONTACT)
    assert _options.await_count == 1
