"""Рендер карточки в HTML для Telegram и клавиатура владельца."""

from __future__ import annotations

import html
from dataclasses import dataclass

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.services.support_assist.card import Card
from app.services.support_assist.flags import Flag


TELEGRAM_LIMIT = 4096

# Строка под карточкой, когда черновик не поместился: «Отправить» в таком случае не рисуется.
TRUNCATED_DRAFT_NOTICE = '⚠️ Черновик слишком длинный и не поместился в карточку целиком. Нажмите «Править».'

_SEV_ICON = {'high': '🔴', 'mid': '⚠️', 'low': 'ℹ️', 'info': '✅', 'unknown': '❓'}


def _e(value: str) -> str:
    return html.escape(value, quote=False)


def flag_facts(flags: list[Flag]) -> list[tuple[str, str]]:
    """Факты карточки без модели: по строке на флаг, значок по тяжести (так же, как их рисует Telegram)."""
    return [(_SEV_ICON.get(f.sev, _SEV_ICON['unknown']), f.text) for f in flags]


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


@dataclass(frozen=True, slots=True)
class RenderedCard:
    """Готовая карточка и признак, что черновик ответа в неё не влез целиком.

    «Отправить» уходит клиенту черновик как есть, поэтому её можно показывать только тогда, когда
    владелец увидел весь черновик: ``draft_truncated`` нужен и клавиатуре, и обработчику кнопки.
    """

    text: str
    draft_truncated: bool


def _fit(card: Card | None, flags: list[Flag], ticket_id: int, limit: int) -> tuple[str, bool]:
    """Текст карточки не длиннее ``limit`` знаков и признак «черновик урезан или выброшен целиком»."""
    if card is None:
        facts = flag_facts(flags)
        cause = owner_action = draft = None
        ask: list[str] = []
        original_draft = ''
    else:
        facts = [(f.icon, f.text) for f in card.facts]
        cause, owner_action, draft = card.cause, card.owner_action, card.draft
        ask = list(card.ask_client)
        original_draft = card.draft or ''

    def build() -> str:
        return _compose(ticket_id, facts, cause, owner_action, ask, draft, no_model=card is None)

    text = build()
    # Порядок урезания: черновик, факты, вопросы, действие, причина.
    while len(text) > limit:
        excess = len(text) - limit
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
            text = text[:limit]
            break
        text = build()
    return text, bool(original_draft) and draft != original_draft


def render_card_checked(
    card: Card | None,
    flags: list[Flag],
    ticket_id: int,
    limit: int = TELEGRAM_LIMIT,
    *,
    truncation_notice: str | None = None,
) -> RenderedCard:
    """HTML карточки не длиннее ``limit`` знаков (по умолчанию лимит Telegram) и признак урезанного черновика.

    Меньший ``limit`` оставляет место под строку решения, которую владелец потом дописывает.
    ``truncation_notice`` дописывается в конец, только если черновик не влез; место под него берётся из ``limit``.
    """
    text, truncated = _fit(card, flags, ticket_id, limit)
    if truncated and truncation_notice:
        suffix = f'\n\n{_e(truncation_notice)}'
        text, truncated = _fit(card, flags, ticket_id, limit - len(suffix))
        text += suffix
    return RenderedCard(text=text, draft_truncated=truncated)


def render_card(card: Card | None, flags: list[Flag], ticket_id: int, limit: int = TELEGRAM_LIMIT) -> str:
    """HTML карточки не длиннее ``limit`` знаков (по умолчанию лимит Telegram)."""
    return render_card_checked(card, flags, ticket_id, limit).text


def build_card_keyboard(card_id: int, show_send: bool, *, draft_truncated: bool = False) -> InlineKeyboardMarkup:
    """«Отправить» нет, если черновик урезан в карточке: владелец не видел его целиком, остаётся «Править»."""
    row: list[InlineKeyboardButton] = []
    if show_send:
        if not draft_truncated:
            row.append(InlineKeyboardButton(text='Отправить', callback_data=f'sa:send:{card_id}'))
        row.append(InlineKeyboardButton(text='Править', callback_data=f'sa:edit:{card_id}'))
    row.append(InlineKeyboardButton(text='Пропустить', callback_data=f'sa:skip:{card_id}'))
    return InlineKeyboardMarkup(inline_keyboard=[row])
