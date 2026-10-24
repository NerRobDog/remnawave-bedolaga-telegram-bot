"""Настройки провайдера Antarctic Wallet."""

import pytest

from app.config import settings


@pytest.mark.parametrize(
    ('app_id', 'secret', 'flag'),
    [(None, 's', True), ('1', None, True), ('1', 's', False)],
)
def test_enabled_requires_id_secret_and_flag(monkeypatch, app_id, secret, flag):
    monkeypatch.setattr(settings, 'ANTARCTIC_APP_ID', app_id)
    monkeypatch.setattr(settings, 'ANTARCTIC_APP_SECRET', secret)
    monkeypatch.setattr(settings, 'ANTARCTIC_ENABLED', flag)
    assert settings.is_antarctic_enabled() is False


def test_enabled_when_all_set(monkeypatch):
    monkeypatch.setattr(settings, 'ANTARCTIC_APP_ID', '1')
    monkeypatch.setattr(settings, 'ANTARCTIC_APP_SECRET', 's')
    monkeypatch.setattr(settings, 'ANTARCTIC_ENABLED', True)
    assert settings.is_antarctic_configured() is True
    assert settings.is_antarctic_enabled() is True


def test_defaults():
    from app.config import Settings

    fields = Settings.model_fields
    assert fields['ANTARCTIC_ENABLED'].default is False
    assert fields['ANTARCTIC_API_BASE'].default == 'https://app.antarcticwallet.com'
    assert fields['ANTARCTIC_MIN_AMOUNT_KOPEKS'].default == 10000
    assert fields['ANTARCTIC_MAX_AMOUNT_KOPEKS'].default == 10000000
    assert fields['ANTARCTIC_PAYMENT_TTL_MINUTES'].default == 30
    assert fields['ANTARCTIC_WEBHOOK_PATH'].default == '/antarctic-webhook'


def test_display_name_falls_back(monkeypatch):
    monkeypatch.setattr(settings, 'ANTARCTIC_DISPLAY_NAME', '   ')
    assert settings.get_antarctic_display_name() == 'Antarctic Wallet'
    monkeypatch.setattr(settings, 'ANTARCTIC_DISPLAY_NAME', ' A<b> ')
    assert settings.get_antarctic_display_name() == 'A<b>'
    assert settings.get_antarctic_display_name_html() == 'A&lt;b&gt;'


def test_app_link_formats_template(monkeypatch):
    monkeypatch.setattr(settings, 'ANTARCTIC_APP_ID', '68019113')
    assert (
        settings.get_antarctic_app_link('abc_DEF-1')
        == 'https://t.me/antarctic_wallet_bot/app?startapp=app_68019113_abc_DEF-1'
    )


@pytest.mark.parametrize(
    ('value', 'expected'),
    [
        ('https://t.me/x?startapp=ref_1', 'https://t.me/x?startapp=ref_1'),
        ('javascript:alert(1)', None),
        ('http://x', None),
        ('', None),
        (None, None),
    ],
)
def test_referral_url_only_https(monkeypatch, value, expected):
    monkeypatch.setattr(settings, 'ANTARCTIC_REFERRAL_URL', value)
    assert settings.get_antarctic_referral_url() == expected


def test_pay_page_url_needs_webhook_url(monkeypatch):
    monkeypatch.setattr(settings, 'WEBHOOK_URL', 'https://hooks.example.com/')
    assert settings.get_antarctic_pay_page_url('tok') == 'https://hooks.example.com/antarctic/pay/tok'
    monkeypatch.setattr(settings, 'WEBHOOK_URL', None)
    assert settings.get_antarctic_pay_page_url('tok') is None
