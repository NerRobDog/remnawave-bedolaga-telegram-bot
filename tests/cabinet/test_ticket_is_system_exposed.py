"""Автовопрос ассистента отличим от ответа человека в админских API.

Сообщение ассистента лежит как ответ админа (is_from_admin=True) с ``is_system=True``. Админский API кабинета и
внешний webapi отдают флаг, чтобы интерфейс мог подписать его «автовопрос». Клиентский API кабинета флаг не отдаёт.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from app.cabinet.routes import admin_tickets as cabinet_admin, tickets as cabinet_user
from app.webapi.routes.tickets import _serialize_message as webapi_serialize


def _message(**overrides) -> SimpleNamespace:
    base = dict(
        id=1,
        user_id=7,
        message_text='Уточните устройство',
        is_from_admin=True,
        is_system=True,
        has_media=False,
        media_type=None,
        media_file_id=None,
        media_caption=None,
        media_items=None,
        created_at=datetime(2026, 11, 7, 10, 0, tzinfo=UTC),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_cabinet_admin_message_carries_is_system():
    assert cabinet_admin._message_to_response(_message()).is_system is True
    assert cabinet_admin._message_to_response(_message(is_system=False)).is_system is False


def test_cabinet_admin_message_without_the_attribute_is_not_system():
    message = _message()
    del message.is_system
    assert cabinet_admin._message_to_response(message).is_system is False


def test_cabinet_admin_detail_and_reply_schemas_use_the_admin_message_model():
    detail = cabinet_admin.AdminTicketDetailResponse.model_fields['messages'].annotation
    assert cabinet_admin.AdminTicketMessageResponse in detail.__args__
    reply_route = next(r for r in cabinet_admin.router.routes if getattr(r, 'path', '').endswith('/{ticket_id}/reply'))
    assert reply_route.response_model is cabinet_admin.AdminTicketMessageResponse


def test_cabinet_user_message_does_not_expose_is_system():
    response = cabinet_user._message_to_response(_message(is_from_admin=True))
    assert 'is_system' not in response.model_dump()


def test_webapi_message_carries_is_system():
    assert webapi_serialize(_message()).is_system is True
    assert webapi_serialize(_message(is_system=False)).is_system is False
