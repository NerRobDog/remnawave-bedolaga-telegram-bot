"""The Anthropic API refuses Russian IPs, so the assistant's client must go through the proxy."""

import anthropic

from app.config import settings
from app.services.support_assist import pipeline


def _capture(monkeypatch):
    seen = {}

    def fake_http_client(**kwargs):
        seen.update(kwargs)

    monkeypatch.setattr(anthropic, 'DefaultAsyncHttpxClient', fake_http_client)
    monkeypatch.setattr(settings, 'ANTHROPIC_API_KEY', 'sk-test')
    return seen


def test_dedicated_proxy_wins(monkeypatch):
    seen = _capture(monkeypatch)
    monkeypatch.setattr(settings, 'SUPPORT_ASSIST_PROXY_URL', 'socks5://assist:1080')
    monkeypatch.setattr(settings, 'PROXY_URL', 'socks5://tg-egress:7890')
    pipeline._anthropic_client()
    assert seen == {'proxy': 'socks5://assist:1080'}


def test_falls_back_to_bot_proxy(monkeypatch):
    seen = _capture(monkeypatch)
    monkeypatch.setattr(settings, 'SUPPORT_ASSIST_PROXY_URL', None)
    monkeypatch.setattr(settings, 'PROXY_URL', 'socks5://tg-egress:7890')
    pipeline._anthropic_client()
    assert seen == {'proxy': 'socks5://tg-egress:7890'}


def test_no_proxy_uses_sdk_default(monkeypatch):
    seen = _capture(monkeypatch)
    monkeypatch.setattr(settings, 'SUPPORT_ASSIST_PROXY_URL', None)
    monkeypatch.setattr(settings, 'PROXY_URL', None)
    client = pipeline._anthropic_client()
    assert seen == {}
    assert isinstance(client, anthropic.AsyncAnthropic)
