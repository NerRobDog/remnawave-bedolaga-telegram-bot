"""Автовопрос ассистента в админском просмотре тикета подписан, а не выдан за ответ поддержки.

Сообщение ассистента хранится как ответ админа (is_from_admin=True) с ``is_system=True``. Без подписи
владелец видел «Поддержка» и мог решить, что клиенту уже ответил человек.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import app.handlers.admin.tickets as admin_tickets
from app.config import Settings


AUTO_ASK = '🤖 автовопрос'


def _message(text: str, *, from_admin: bool, system: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        created_at=datetime(2026, 11, 7, 10, 0, tzinfo=UTC),
        message_text=text,
        is_user_message=not from_admin,
        is_from_admin=from_admin,
        is_system=system,
        has_media=False,
        media_type=None,
        media_file_id=None,
        media_items=None,
    )


def _ticket(messages: list[SimpleNamespace]) -> SimpleNamespace:
    user = SimpleNamespace(id=42, telegram_id=777, username=None, full_name='Клиент', email=None, language='ru')
    return SimpleNamespace(
        id=17,
        user_id=42,
        user=user,
        title='Не работает',
        status='open',
        status_emoji='🟢',
        is_closed=False,
        is_user_reply_blocked=False,
        user_reply_block_permanent=False,
        user_reply_block_until=None,
        created_at=datetime(2026, 11, 7, 9, 0, tzinfo=UTC),
        messages=messages,
    )


def test_sender_label_marks_system_messages():
    assert admin_tickets._message_sender_label(_message('q', from_admin=True, system=True)) == AUTO_ASK
    assert admin_tickets._message_sender_label(_message('q', from_admin=True)) == '🛠️ Поддержка'
    assert admin_tickets._message_sender_label(_message('q', from_admin=False)) == '👤 Пользователь'


@pytest.mark.asyncio
async def test_admin_view_labels_the_auto_ask_message():
    ticket = _ticket(
        [
            _message('не работает', from_admin=False),
            _message('Уточните, пожалуйста, устройство', from_admin=True, system=True),
            _message('Сейчас посмотрю', from_admin=True),
        ]
    )
    callback = SimpleNamespace(
        data='admin_ticket_17', from_user=SimpleNamespace(id=1), message=SimpleNamespace(), answer=AsyncMock()
    )

    with (
        patch.object(Settings, 'is_admin', lambda self, *a, **k: True),
        patch.object(admin_tickets.TicketCRUD, 'get_ticket_by_id', AsyncMock(return_value=ticket)),
        patch.object(admin_tickets, 'safe_edit_or_resend', AsyncMock()) as send,
    ):
        await admin_tickets.view_admin_ticket(
            callback, SimpleNamespace(id=1, language='ru'), AsyncMock(), state=AsyncMock(), ticket_id=17
        )

    shown = send.await_args.args[1]
    assert f'{AUTO_ASK} (' in shown
    assert shown.count('🛠️ Поддержка') == 1  # настоящий ответ остался подписан поддержкой
