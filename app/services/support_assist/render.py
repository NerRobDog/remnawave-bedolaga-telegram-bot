"""Рендер карточки в HTML для Telegram и клавиатура владельца."""

from __future__ import annotations

import html

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.services.support_assist.card import Card
from app.services.support_assist.flags import Flag


TELEGRAM_LIMIT = 4096

_SEV_ICON = {'high': '🔴', 'mid': '⚠️', 'low': '✅', 'info': '✅'}


def _e(value: str) -> str:
    return html.escape(value, quote=False)


def _compose(
    ticket_id: int,
    facts: list[tuple[str, str]],
    cause: str | None,
    owner_action: str | None,
    ask: list[str],
    draft: str | None,
    *,
    no_model: bool,
) -> str:
    head = f'<b>Тикет #{ticket_id}</b>'
    if no_model:
        head += ' · разбор без модели'
    parts = [head]
    if facts:
        parts.append('🔎 <b>Разбор</b>\n' + '\n'.join(f'{icon} {_e(text)}' for icon, text in facts))
    if cause:
        parts.append(f'💡 <b>Вероятная причина:</b> {_e(cause)}')
    if owner_action:
        parts.append(f'🛠 <b>Действие владельца:</b> {_e(owner_action)}')
    if ask:
        parts.append('❓ <b>Спросить у клиента:</b>\n' + '\n'.join(f'• {_e(q)}' for q in ask))
    if draft:
        parts.append(f'✏️ <b>Черновик ответа клиенту:</b>\n{_e(draft)}')
    return '\n\n'.join(parts)


def _cut(value: str, excess: int) -> str:
    """Укоротить строку не меньше чем на ``excess`` знаков (с запасом на экранирование)."""
    keep = max(0, len(value) - excess - 8)
    return value[:keep].rstrip() + ('…' if keep else '')


def render_card(card: Card | None, flags: list[Flag], ticket_id: int) -> str:
    if card is None:
        facts = [(_SEV_ICON[f.sev], f.text) for f in flags]
        cause = owner_action = draft = None
        ask: list[str] = []
    else:
        facts = [(f.icon, f.text) for f in card.facts]
        cause, owner_action, draft = card.cause, card.owner_action, card.draft
        ask = list(card.ask_client)

    def build() -> str:
        return _compose(ticket_id, facts, cause, owner_action, ask, draft, no_model=card is None)

    text = build()
    # Порядок урезания: черновик, факты, вопросы, действие, причина.
    while len(text) > TELEGRAM_LIMIT:
        excess = len(text) - TELEGRAM_LIMIT
        if draft:
            draft = _cut(draft, excess) or None
        elif facts:
            icon, body = facts[-1]
            shorter = _cut(body, excess)
            if shorter:
                facts[-1] = (icon, shorter)
            else:
                facts.pop()
        elif ask:
            shorter = _cut(ask[-1], excess)
            if shorter:
                ask[-1] = shorter
            else:
                ask.pop()
        elif owner_action:
            owner_action = _cut(owner_action, excess) or None
        elif cause:
            cause = _cut(cause, excess) or None
        else:
            return text[:TELEGRAM_LIMIT]
        text = build()
    return text


def build_card_keyboard(card_id: int, show_send: bool) -> InlineKeyboardMarkup:
    row: list[InlineKeyboardButton] = []
    if show_send:
        row.append(InlineKeyboardButton(text='Отправить', callback_data=f'sa:send:{card_id}'))
        row.append(InlineKeyboardButton(text='Править', callback_data=f'sa:edit:{card_id}'))
    row.append(InlineKeyboardButton(text='Пропустить', callback_data=f'sa:skip:{card_id}'))
    return InlineKeyboardMarkup(inline_keyboard=[row])
