"""Обязательная анкета перед тикетом: поля, проверка ответов, показ владельцу.

Ответ клиента хранится в ``Ticket.diagnostics`` как JSON:
``{'platform': 'ios', 'app': 'Clash Mi', 'location': 'russia', 'network': 'wifi', ...}``.

Устройство и приложение берутся из конфига страницы подписки Remnawave (``form_options``):
``platform`` — id платформы из конфига или ``router`` / ``other``, ``app`` — название приложения
или ``other``. Остальные поля — статичные id вариантов, тексты которых живут в локалях
(``TICKET_FORM_<FIELD>_<OPTION>``).

Свободный текст допустим только в «Другое»: ``<field>_other``, до ``OTHER_MAX_LENGTH`` знаков.
"""

from __future__ import annotations

from typing import Any

from app.localization.texts import get_texts
from app.services.support_assist.form_options import (
    LOCATIONS,
    FormOptions,
    fallback_options,
    is_app_name,
    is_platform_id,
)


# Порядок полей — порядок вопросов в боте.
FORM_FIELD_IDS: tuple[str, ...] = ('location', 'platform', 'app', 'network', 'symptom', 'since')

# Поля со статичными вариантами (id вариантов и тексты в локалях).
STATIC_OPTIONS: dict[str, tuple[str, ...]] = {
    'location': LOCATIONS,
    'network': ('mobile', 'wifi', 'both'),
    'symptom': ('nothing', 'specific_site', 'slow', 'no_connect'),
    'since': ('today', 'days', 'always', 'after_update'),
}

# Поля с вариантом «Другое» и свободным текстом к нему.
OTHER_FIELDS: tuple[str, ...] = ('platform', 'app')

OTHER_OPTION = 'other'
OTHER_MAX_LENGTH = 100

# Какой автовопрос (``questions.QUESTION_IDS``) закрывает поле анкеты. У ``location`` такого нет.
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
    return field in OTHER_FIELDS


def clean_other_text(value: str) -> str:
    """Короткий текст «Другое»: пробелы и переводы строк схлопнуты, длина не больше ``OTHER_MAX_LENGTH``."""
    return ' '.join(value.split())[:OTHER_MAX_LENGTH].strip()


def looks_valid(field: str, value: Any) -> bool:
    """Значение поля по форме, без сверки с текущим конфигом (для сохранённых ответов и нагрузки)."""
    if not isinstance(value, str):
        return False
    if field == 'platform':
        return is_platform_id(value)
    if field == 'app':
        return value == OTHER_OPTION or is_app_name(value)
    return value in STATIC_OPTIONS.get(field, ())


def answered_fields(raw: Any) -> set[str]:
    """Поля, на которые в сохранённой анкете есть годный по форме ответ (мусор в колонке не считается)."""
    if not isinstance(raw, dict):
        return set()
    return {field for field in FORM_FIELD_IDS if looks_valid(field, raw.get(field))}


def _accepts(field: str, value: str, options: FormOptions) -> bool:
    if field == 'platform':
        if value in options.platform_ids():
            return True
        # Конфиг сейчас недоступен, а клиент мог загрузить варианты раньше: принимаем любой годный id.
        return not options.from_config and is_platform_id(value)
    if field == 'app':
        if value == OTHER_OPTION or value in options.all_apps():
            return True
        return not options.from_config and is_app_name(value)
    return value in STATIC_OPTIONS[field]


def validate_diagnostics(raw: dict | None, options: FormOptions) -> dict:
    """Проверить ответы анкеты и вернуть их в каноническом виде.

    ``ValueError`` — анкеты нет, поле пропущено или вариант не из списка. Лишние ключи отбрасываются.
    Текст «Другое» берётся только если «Другое» выбрано, и тогда он обязателен (как и в боте): пустой
    или из одних пробелов отклоняется.
    """
    if not isinstance(raw, dict):
        raise ValueError('diagnostics form is required')
    out: dict[str, str] = {}
    for field in FORM_FIELD_IDS:
        value = raw.get(field)
        if value is None or value == '':
            raise ValueError(f'diagnostics.{field} is required')
        if not isinstance(value, str) or not _accepts(field, value, options):
            raise ValueError(f'diagnostics.{field}: unknown option')
        out[field] = value
        if value == OTHER_OPTION and has_other(field):
            text = raw.get(other_key(field))
            if not isinstance(text, str):
                raise ValueError(f'diagnostics.{other_key(field)} must be a string')
            text = clean_other_text(text)
            if not text:
                raise ValueError(f'diagnostics.{other_key(field)} is required when "{OTHER_OPTION}" is chosen')
            out[other_key(field)] = text
    return out


def platform_label(platform: str, lang: str, options: FormOptions) -> str:
    """Имя платформы: из конфига, иначе из локали (``router``, ``other``), иначе сам id."""
    name = options.platform_name(platform, lang)
    if name:
        return name
    return get_texts(lang).t(f'TICKET_FORM_PLATFORM_{platform.upper()}', platform)


def render_diagnostics(d: dict, lang: str, options: FormOptions | None = None) -> str:
    """Ответы анкеты построчно «Поле: вариант» для уведомления владельцу и карточки.

    Простой текст, без HTML: свободный текст клиента экранирует вызывающий. Неизвестные
    поля и негодные значения (старые или повреждённые данные) пропускаются.
    """
    if not isinstance(d, dict):
        return ''
    options = options or fallback_options()
    texts = get_texts(lang)
    lines: list[str] = []
    for field in FORM_FIELD_IDS:
        value = d.get(field)
        if not looks_valid(field, value):
            continue
        if value == OTHER_OPTION and has_other(field):
            shown = texts.t(f'TICKET_FORM_{field.upper()}_OTHER')
        elif field == 'platform':
            shown = platform_label(value, lang, options)
        elif field == 'app':
            shown = value
        else:
            shown = texts.t(f'TICKET_FORM_{field.upper()}_{value.upper()}')
        extra = d.get(other_key(field)) if value == OTHER_OPTION else None
        if isinstance(extra, str) and extra.strip():
            shown = f'{shown} — {clean_other_text(extra)}'
        lines.append(f'{texts.t(f"TICKET_FORM_{field.upper()}_LABEL")}: {shown}')
    return '\n'.join(lines)
