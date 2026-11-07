"""Варианты анкеты «устройство» и «приложение» из конфига страницы подписки Remnawave.

Источник тот же, что у страницы подключения в кабинете (``_load_app_config_async``):
``config['platforms'][key] = {'displayName': {'ru': .., 'en': ..}, 'apps': [{'name': ..}, ..]}``.
Платформы идут в порядке конфига, за ними всегда ``router`` и ``other``. К приложениям каждой
платформы добавляется Happ (старое приложение, пока его не вывели). Конфиг недоступен — запасной
статичный набор, а проверка ответов становится терпимее (``from_config=False``).

Собранные варианты живут в памяти процесса ``CACHE_TTL_SECONDS``: support-config кабинета
запрашивают на каждое открытие страницы.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

import structlog


logger = structlog.get_logger(__name__)

CACHE_TTL_SECONDS = 300.0

LEGACY_APP = 'Happ'
EXTRA_PLATFORMS: tuple[str, ...] = ('router', 'other')
LOCATIONS: tuple[str, ...] = ('russia', 'abroad')

PLATFORM_ID_RE = re.compile(r'^[A-Za-z][A-Za-z0-9_-]{0,31}$')
APP_NAME_MAX_LENGTH = 40
_CONTROL_RE = re.compile(r'[\x00-\x1f\x7f-\x9f  ]')

# Запасные платформы и статичные имена (и для платформ конфига без displayName).
FALLBACK_PLATFORMS: tuple[str, ...] = ('ios', 'android', 'windows', 'macos')
STATIC_PLATFORM_NAMES: dict[str, dict[str, str]] = {
    'ios': {'ru': 'iOS', 'en': 'iOS'},
    'android': {'ru': 'Android', 'en': 'Android'},
    'windows': {'ru': 'Windows', 'en': 'Windows'},
    'macos': {'ru': 'macOS', 'en': 'macOS'},
    'linux': {'ru': 'Linux', 'en': 'Linux'},
    'androidTV': {'ru': 'Android TV', 'en': 'Android TV'},
    'appleTV': {'ru': 'Apple TV', 'en': 'Apple TV'},
}
FALLBACK_APPS: tuple[str, ...] = ('Clash Mi', 'Happ', 'RabbitHole', 'FlClashX')


def is_platform_id(value: Any) -> bool:
    """Строка похожа на id платформы (как ключи конфига: ``ios``, ``appleTV``)."""
    return isinstance(value, str) and bool(PLATFORM_ID_RE.match(value))


def is_app_name(value: Any) -> bool:
    """Строка годится как название приложения: 1..40 знаков, без управляющих символов и переводов строк."""
    return (
        isinstance(value, str)
        and 0 < len(value) <= APP_NAME_MAX_LENGTH
        and bool(value.strip())
        and not _CONTROL_RE.search(value)
    )


@dataclass(frozen=True)
class FormOptions:
    """Варианты устройства и приложения. ``platforms`` — пары (id, имя по языкам или ``None``)."""

    platforms: tuple[tuple[str, dict[str, str] | None], ...]
    apps: dict[str, tuple[str, ...]] = field(default_factory=dict)
    from_config: bool = False

    def platform_ids(self) -> tuple[str, ...]:
        return tuple(pid for pid, _ in self.platforms)

    def all_apps(self) -> tuple[str, ...]:
        """Все названия приложений без повторов в порядке появления; Happ последним."""
        return _with_legacy(name for names in self.apps.values() for name in names if name != LEGACY_APP)

    def apps_for(self, platform: str) -> tuple[str, ...]:
        """Приложения платформы; неизвестная платформа — все приложения."""
        return self.apps.get(platform) or self.all_apps()

    def platform_name(self, platform: str, lang: str) -> str | None:
        """Имя платформы на языке клиента (иначе en, иначе ru); ``None`` — переводит вызывающий."""
        for pid, name in self.platforms:
            if pid == platform and name:
                return name.get(lang) or name.get('en') or name.get('ru') or next(iter(name.values()), None)
        return None

    def to_api(self) -> dict[str, Any]:
        """Вид для ``GET /cabinet/info/support-config`` (поле ``ticket_form_options``)."""
        return {
            'platforms': [{'id': pid, 'name': dict(name) if name else None} for pid, name in self.platforms],
            'apps': {pid: list(names) for pid, names in self.apps.items()},
            'locations': list(LOCATIONS),
        }


def _dedup(items) -> tuple[str, ...]:
    return tuple(dict.fromkeys(items))


def _with_legacy(names) -> tuple[str, ...]:
    out = _dedup(names)
    return out if LEGACY_APP in out else (*out, LEGACY_APP)


def _display_name(pid: str, raw: Any) -> dict[str, str]:
    if isinstance(raw, dict):
        name = {
            str(lang): value.strip()
            for lang, value in raw.items()
            if isinstance(lang, str) and len(lang) <= 10 and is_app_name(value)
        }
        if name:
            return name
    if isinstance(raw, str) and is_app_name(raw):
        return {'ru': raw.strip(), 'en': raw.strip()}
    return dict(STATIC_PLATFORM_NAMES.get(pid) or {'ru': pid, 'en': pid})


def fallback_options() -> FormOptions:
    platforms = tuple((pid, dict(STATIC_PLATFORM_NAMES[pid])) for pid in FALLBACK_PLATFORMS)
    platforms += tuple((pid, None) for pid in EXTRA_PLATFORMS)
    return FormOptions(
        platforms=platforms,
        apps={pid: FALLBACK_APPS for pid, _ in platforms},
        from_config=False,
    )


def build_form_options(config: Any) -> FormOptions:
    """Варианты из конфига страницы подписки; негодный или пустой конфиг — запасной набор."""
    raw_platforms = config.get('platforms') if isinstance(config, dict) else None
    if not isinstance(raw_platforms, dict):
        return fallback_options()

    platforms: list[tuple[str, dict[str, str] | None]] = []
    apps: dict[str, tuple[str, ...]] = {}
    for pid, data in raw_platforms.items():
        if not is_platform_id(pid) or pid in EXTRA_PLATFORMS or pid in apps or not isinstance(data, dict):
            continue
        raw_apps = data.get('apps')
        names = [
            app['name'].strip()
            for app in (raw_apps if isinstance(raw_apps, list) else [])
            if isinstance(app, dict) and is_app_name(app.get('name'))
        ]
        platforms.append((pid, _display_name(pid, data.get('displayName'))))
        apps[pid] = _with_legacy(names)

    if not platforms:
        return fallback_options()

    union = _with_legacy(name for names in apps.values() for name in names if name != LEGACY_APP)
    for pid in EXTRA_PLATFORMS:
        platforms.append((pid, None))
        apps[pid] = union
    return FormOptions(platforms=tuple(platforms), apps=apps, from_config=True)


async def _load_config() -> dict[str, Any] | None:
    # Тот же загрузчик, что у страницы подключения кабинета; импорт ленивый, чтобы сервис не тянул роуты.
    from app.cabinet.routes.subscription_modules.status import _load_app_config_async

    return await _load_app_config_async()


def _now() -> float:
    return time.monotonic()


_cache: tuple[float, FormOptions] | None = None


def reset_cache() -> None:
    global _cache
    _cache = None


async def get_form_options() -> FormOptions:
    """Варианты анкеты из кэша процесса; ошибка Remnawave даёт запасной набор, а не исключение."""
    global _cache
    now = _now()
    if _cache is not None and now - _cache[0] < CACHE_TTL_SECONDS:
        return _cache[1]
    try:
        options = build_form_options(await _load_config())
    except Exception as exc:
        logger.warning('Ticket form options: subscription page config unavailable, using fallback', error=str(exc))
        options = fallback_options()
    if not options.from_config:
        logger.info('Ticket form options: using the static fallback')
    _cache = (now, options)
    return options
