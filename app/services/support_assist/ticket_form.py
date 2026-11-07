"""Обязательная анкета перед тикетом: поля, проверка ответов, показ владельцу.

Ответ клиента хранится в ``Ticket.diagnostics`` как JSON с id вариантов (не с текстами):
``{'platform': 'ios', 'app': 'happ', ...}``. Тексты вариантов живут в локалях
(``TICKET_FORM_<FIELD>_<OPTION>``), поэтому владелец видит анкету на своём языке, а ассистент
получает стабильные id.

Свободный текст допустим только в «Другое»: ``<field>_other``, до ``OTHER_MAX_LENGTH`` знаков.
"""

from __future__ import annotations

from typing import Any

from app.localization.texts import get_texts


# Порядок полей — порядок вопросов в боте и в кабинете.
FORM_FIELDS: dict[str, tuple[str, ...]] = {
    'platform': ('ios', 'android', 'windows', 'macos', 'router', 'other'),
    'app': ('clash_mi', 'happ', 'rabbit_hole', 'flclash_x', 'other'),
    'network': ('mobile', 'wifi', 'both'),
    'symptom': ('nothing', 'specific_site', 'slow', 'no_connect'),
    'since': ('today', 'days', 'always', 'after_update'),
}

OTHER_OPTION = 'other'
OTHER_MAX_LENGTH = 100

# Какой автовопрос (``questions.QUESTION_IDS``) закрывает поле анкеты.
ANSWERED_QUESTIONS: dict[str, str] = {
    'platform': 'device',
    'app': 'app',
    'network': 'network',
    'symptom': 'sites',
    'since': 'since_when',
}


def other_key(field: str) -> str:
    """Ключ свободного текста для поля, где выбрано «Другое»."""
    return f'{field}_other'


def has_other(field: str) -> bool:
    """Есть ли у поля вариант «Другое»."""
    return OTHER_OPTION in FORM_FIELDS[field]


def clean_other_text(value: str) -> str:
    """Короткий текст «Другое»: пробелы и переводы строк схлопнуты, длина не больше ``OTHER_MAX_LENGTH``."""
    return ' '.join(value.split())[:OTHER_MAX_LENGTH].strip()


def answered_fields(raw: Any) -> set[str]:
    """Поля, на которые в сохранённой анкете есть ответ из списка вариантов (мусор в колонке не считается)."""
    if not isinstance(raw, dict):
        return set()
    return {
        field for field, options in FORM_FIELDS.items() if isinstance(raw.get(field), str) and raw[field] in options
    }


def validate_diagnostics(raw: dict | None) -> dict:
    """Проверить ответы анкеты и вернуть их в каноническом виде.

    ``ValueError`` — анкеты нет, поле пропущено или вариант не из списка. Лишние ключи отбрасываются.
    Текст «Другое» берётся только если «Другое» выбрано, и тогда он обязателен (как и в боте): пустой
    или из одних пробелов отклоняется.
    """
    if not isinstance(raw, dict):
        raise ValueError('diagnostics form is required')
    out: dict[str, str] = {}
    for field, options in FORM_FIELDS.items():
        value = raw.get(field)
        if value is None or value == '':
            raise ValueError(f'diagnostics.{field} is required')
        if not isinstance(value, str) or value not in options:
            raise ValueError(f'diagnostics.{field}: unknown option')
        out[field] = value
        if value == OTHER_OPTION:
            text = raw.get(other_key(field))
            if not isinstance(text, str):
                raise ValueError(f'diagnostics.{other_key(field)} must be a string')
            text = clean_other_text(text)
            if not text:
                raise ValueError(f'diagnostics.{other_key(field)} is required when "{OTHER_OPTION}" is chosen')
            out[other_key(field)] = text
    return out


def render_diagnostics(d: dict, lang: str) -> str:
    """Ответы анкеты построчно «Поле: вариант» для уведомления владельцу и карточки.

    Простой текст, без HTML: свободный текст клиента экранирует вызывающий. Неизвестные
    поля и варианты (старые или повреждённые данные) пропускаются.
    """
    if not isinstance(d, dict):
        return ''
    texts = get_texts(lang)
    lines: list[str] = []
    for field, options in FORM_FIELDS.items():
        value = d.get(field)
        if not isinstance(value, str) or value not in options:
            continue
        shown = texts.t(f'TICKET_FORM_{field.upper()}_{value.upper()}')
        extra = d.get(other_key(field)) if value == OTHER_OPTION else None
        if isinstance(extra, str) and extra.strip():
            shown = f'{shown} — {clean_other_text(extra)}'
        lines.append(f'{texts.t(f"TICKET_FORM_{field.upper()}_LABEL")}: {shown}')
    return '\n'.join(lines)
