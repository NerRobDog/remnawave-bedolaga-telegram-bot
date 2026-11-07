"""Уведомление о бонусе за привязку способа входа: каналы, тексты, и то, что сбой доставки ничего не ломает."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.cabinet.services.email_templates import EmailNotificationTemplates
from app.localization.texts import get_texts
from app.services import auth_link_bonus_notifications as notifications
from app.services.auth_link_bonus_service import LinkBonusGrant
from app.services.notification_types import NotificationType


LOCALES = Path(__file__).resolve().parents[2] / 'app' / 'localization' / 'locales'
GRANT = LinkBonusGrant(
    user_id=5, method='passkey', days=7, subscription_id=11, new_end_date=datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
)


def _user(**fields) -> SimpleNamespace:
    base = dict(id=5, telegram_id=None, language='ru', email=None, email_verified=False, status='active')
    base.update(fields)
    return SimpleNamespace(**base)


@pytest.fixture
def wired(monkeypatch):
    """Подмена всех внешних каналов: доставка, WebSocket кабинета, панель, бот."""
    delivery = AsyncMock(return_value=True)
    monkeypatch.setattr(
        'app.services.notification_delivery_service.notification_delivery_service.send_notification', delivery
    )
    ws = AsyncMock()
    monkeypatch.setattr('app.cabinet.routes.websocket.cabinet_ws_manager.send_to_user', ws)
    sync = AsyncMock()
    monkeypatch.setattr('app.services.subscription_service.SubscriptionService.sync_remnawave_user', sync)
    bot = MagicMock()
    bot.session.close = AsyncMock()
    monkeypatch.setattr('app.bot_factory.create_bot', MagicMock(return_value=bot))
    monkeypatch.setattr('app.config.settings.BOT_TOKEN', '123:test')
    return SimpleNamespace(delivery=delivery, ws=ws, sync=sync, bot=bot)


def _db_with_subscription() -> AsyncMock:
    db = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.first.return_value = SimpleNamespace(id=GRANT.subscription_id)
    db.execute.return_value = result
    return db


@pytest.mark.asyncio
async def test_a_user_with_telegram_gets_a_telegram_message_and_a_cabinet_event(wired) -> None:
    await notifications.deliver_link_bonus(_db_with_subscription(), _user(telegram_id=1001), GRANT)

    wired.delivery.assert_awaited_once()
    kwargs = wired.delivery.await_args.kwargs
    assert kwargs['notification_type'] is NotificationType.AUTH_LINK_BONUS
    assert kwargs['bot'] is wired.bot
    assert '7' in kwargs['telegram_message'] and '17.11.2026' in kwargs['telegram_message']
    assert kwargs['context']['days'] == 7
    wired.ws.assert_awaited_once()
    user_id, event = wired.ws.await_args.args
    assert user_id == 5
    assert event['type'] == 'auth_link_bonus.granted' and event['days'] == 7
    assert event['new_expires_at'].startswith('2026-11-17T12:00')
    wired.bot.session.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_user_without_telegram_gets_no_bot_and_still_the_cabinet_event(wired) -> None:
    await notifications.deliver_link_bonus(
        _db_with_subscription(), _user(email='a@example.com', email_verified=True), GRANT
    )

    assert wired.delivery.await_args.kwargs['bot'] is None
    wired.ws.assert_awaited_once()


@pytest.mark.asyncio
async def test_the_subscription_is_synced_with_the_panel(wired) -> None:
    await notifications.deliver_link_bonus(_db_with_subscription(), _user(), GRANT)

    wired.sync.assert_awaited_once()
    assert wired.sync.await_args.args[1].id == GRANT.subscription_id


@pytest.mark.asyncio
async def test_a_failing_panel_sync_does_not_stop_the_notification(wired) -> None:
    wired.sync.side_effect = RuntimeError('panel down')

    await notifications.deliver_link_bonus(_db_with_subscription(), _user(telegram_id=1001), GRANT)

    wired.delivery.assert_awaited_once()
    wired.ws.assert_awaited_once()


class _ExpirableUser(SimpleNamespace):
    """Как ORM-объект после отката сессии: любое чтение поля бросает (в async-сессии это MissingGreenlet)."""

    def expire(self) -> None:
        object.__setattr__(self, '_expired', True)

    def __getattribute__(self, name):
        if not name.startswith('_') and name != 'expire' and object.__getattribute__(self, '__dict__').get('_expired'):
            raise RuntimeError(f'MissingGreenlet: {name} read on an expired instance')
        return object.__getattribute__(self, name)


@pytest.mark.asyncio
async def test_the_notification_survives_a_panel_sync_that_expires_the_user(wired) -> None:
    user = _ExpirableUser(
        id=5, telegram_id=1001, language='en', email='a@example.com', email_verified=True, status='active'
    )
    wired.sync.side_effect = lambda *_a, **_k: (user.expire(), (_ for _ in ()).throw(RuntimeError('panel down')))[1]
    seen = {}

    async def deliver(**kwargs):
        target = kwargs['user']
        seen.update(
            id=target.id,
            status=target.status,
            telegram_id=target.telegram_id,
            language=target.language,
            email=target.email,
            email_verified=target.email_verified,
        )
        return True

    wired.delivery.side_effect = deliver

    await notifications.deliver_link_bonus(_db_with_subscription(), user, GRANT)

    wired.delivery.assert_awaited_once()
    assert seen == dict(
        id=5, status='active', telegram_id=1001, language='en', email='a@example.com', email_verified=True
    )
    assert wired.delivery.await_args.kwargs['bot'] is wired.bot


@pytest.mark.asyncio
async def test_a_failing_delivery_never_raises(wired) -> None:
    wired.delivery.side_effect = RuntimeError('telegram down')
    wired.ws.side_effect = RuntimeError('ws down')

    await notifications.deliver_link_bonus(_db_with_subscription(), _user(telegram_id=1001), GRANT)


@pytest.mark.asyncio
async def test_a_failing_database_read_never_raises(wired) -> None:
    db = AsyncMock()
    db.execute.side_effect = RuntimeError('db down')

    await notifications.deliver_link_bonus(db, _user(telegram_id=1001), GRANT)

    wired.delivery.assert_awaited_once()


# -- тексты --------------------------------------------------------------


@pytest.mark.parametrize('lang', ['ru', 'en', 'ua', 'zh', 'fa'])
def test_every_locale_has_the_text_with_the_same_placeholders(lang) -> None:
    data = json.loads((LOCALES / f'{lang}.json').read_text(encoding='utf-8'))
    text = data['AUTH_LINK_BONUS_GRANTED']
    assert '{days}' in text and '{until}' in text
    assert text.format(days=7, until='17.11.2026 12:00')
    assert get_texts(lang).get('AUTH_LINK_BONUS_GRANTED') == text


def test_the_texts_name_no_deployment() -> None:
    for lang in ['ru', 'en', 'ua', 'zh', 'fa']:
        text = json.loads((LOCALES / f'{lang}.json').read_text(encoding='utf-8'))['AUTH_LINK_BONUS_GRANTED']
        assert 'atchdog' not in text and 'watchd0g' not in text


@pytest.mark.parametrize('lang', ['ru', 'en', 'ua', 'zh'])
def test_the_email_template_renders_days_and_date(lang) -> None:
    rendered = EmailNotificationTemplates().get_template(
        NotificationType.AUTH_LINK_BONUS, lang, {'days': 7, 'until': '17.11.2026 12:00'}
    )

    assert rendered and rendered['subject']
    assert '7' in rendered['body_html'] and '17.11.2026 12:00' in rendered['body_html']


def test_the_email_template_escapes_the_date() -> None:
    rendered = EmailNotificationTemplates().get_template(
        NotificationType.AUTH_LINK_BONUS, 'en', {'days': 7, 'until': '<script>alert(1)</script>'}
    )

    assert '<script>' not in rendered['body_html']
