"""Переключатель «Я в РФ / Я за границей»: смена внешнего сквада пользователя в панели.

Внешний сквад у пользователя уже может стоять (сквад тарифа, ручной ``Test 1`` /
``NoHWID``), поэтому «РФ» — это не ``null``, а возврат прежнего («домашнего») сквада.
Домашний сквад читается из панели перед записью и хранится в
``Subscription.abroad_home_squad_uuid`` до выхода из режима.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog

from app.config import settings
from app.external.remnawave_errors import RemnaWaveAPIError


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.database.models import Subscription


logger = structlog.get_logger(__name__)


class AbroadModeError(Exception):
    """Базовая ошибка; ``code`` уходит в API как есть."""

    code = 'abroad_mode_error'


class AbroadModeDisabled(AbroadModeError):
    code = 'abroad_mode_disabled'


class AbroadModeUnavailable(AbroadModeError):
    """У подписки нет панельной учётки или она не активна."""

    code = 'abroad_mode_unavailable'


class AbroadModePanelError(AbroadModeError):
    code = 'panel_error'


@dataclass(frozen=True)
class AbroadModeState:
    abroad: bool
    changed: bool = False


def abroad_squad_uuid() -> str | None:
    value = (settings.ABROAD_EXTERNAL_SQUAD_UUID or '').strip()
    return value or None


def is_available() -> bool:
    return bool(settings.ABROAD_MODE_ENABLED and abroad_squad_uuid())


def resolve_external_squad_for_sync(subscription: Subscription, tariff_squad: str | None) -> str | None:
    """Сквад, который обычная синхронизация с панелью должна записать.

    В режиме «за границей» синхронизация (смена тарифа, пересоздание) не должна
    затирать сквад Abroad сквадом тарифа.
    """
    if getattr(subscription, 'abroad_mode', False) and is_available():
        return abroad_squad_uuid()
    return tariff_squad


def _normalize(value: str | None) -> str | None:
    value = (value or '').strip().lower()
    return value or None


def _home_target(subscription: Subscription) -> str | None:
    """Куда вернуть при выходе: актуальный сквад тарифа, иначе сохранённый домашний."""
    tariff = getattr(subscription, 'tariff', None)
    tariff_squad = getattr(tariff, 'external_squad_uuid', None) if tariff else None
    return (tariff_squad or subscription.abroad_home_squad_uuid or '').strip() or None


async def _write_squad(api: Any, panel_id: int, target: str | None) -> None:
    """Записать сквад и убедиться, что панель его приняла.

    ``update_user`` при «протухшем» сквадe молча повторяет запрос без поля и
    возвращает успех, поэтому результат сверяем с запрошенным.
    """
    # Лениво: writer тянет payload, а payload — этот модуль (цикл импорта).
    from app.services.panel_sync.writer import patch_panel_external_squad

    try:
        updated = await patch_panel_external_squad(api, user_id=panel_id, external_squad_uuid=target)
    except RemnaWaveAPIError as error:
        raise AbroadModePanelError(str(error)) from error
    if _normalize(updated.external_squad_uuid) != _normalize(target):
        raise AbroadModePanelError('panel did not apply external squad')


async def get_state(api: Any, subscription: Subscription) -> bool:
    """Живое состояние из панели: стоит ли сейчас сквад Abroad."""
    panel_id = _panel_id(subscription)
    try:
        user = await api.get_user_by_id(panel_id)
    except RemnaWaveAPIError as error:
        raise AbroadModePanelError(str(error)) from error
    if user is None:
        raise AbroadModePanelError('panel user not found')
    return _normalize(user.external_squad_uuid) == _normalize(abroad_squad_uuid())


def _panel_id(subscription: Subscription) -> int:
    if not getattr(subscription, 'remnawave_id', None):
        raise AbroadModeUnavailable('subscription has no panel account')
    return int(subscription.remnawave_id)


async def set_abroad_mode(
    db: AsyncSession,
    api: Any,
    subscription: Subscription,
    enabled: bool,
) -> AbroadModeState:
    if not is_available():
        raise AbroadModeDisabled('abroad mode is disabled')
    panel_id = _panel_id(subscription)
    abroad_uuid = abroad_squad_uuid()

    try:
        panel_user = await api.get_user_by_id(panel_id)
    except RemnaWaveAPIError as error:
        raise AbroadModePanelError(str(error)) from error
    if panel_user is None:
        raise AbroadModePanelError('panel user not found')
    current = _normalize(panel_user.external_squad_uuid)
    on_abroad = current == _normalize(abroad_uuid)

    if enabled:
        if on_abroad:
            # Уже в панели; домашний сквад не трогаем — он мог быть сохранён раньше.
            if not subscription.abroad_mode:
                subscription.abroad_mode = True
                await db.commit()
            return AbroadModeState(abroad=True)

        previous_mode = subscription.abroad_mode
        previous_home = subscription.abroad_home_squad_uuid
        # Сначала фиксируем домашний сквад, потом пишем в панель: иначе сбой между
        # двумя шагами оставит пользователя на Abroad без пути назад.
        subscription.abroad_mode = True
        subscription.abroad_home_squad_uuid = (panel_user.external_squad_uuid or '').strip() or None
        await db.commit()
        try:
            await _write_squad(api, panel_id, abroad_uuid)
        except AbroadModeError:
            subscription.abroad_mode = previous_mode
            subscription.abroad_home_squad_uuid = previous_home
            await db.commit()
            raise
        logger.info('Abroad mode on', subscription_id=subscription.id, home_squad=subscription.abroad_home_squad_uuid)
        return AbroadModeState(abroad=True, changed=True)

    # Выход из режима.
    if not on_abroad:
        # Админ уже сменил сквад руками — чужую настройку не перезаписываем.
        changed = subscription.abroad_mode
        subscription.abroad_mode = False
        subscription.abroad_home_squad_uuid = None
        await db.commit()
        return AbroadModeState(abroad=False, changed=changed)

    target = _home_target(subscription)
    try:
        await _write_squad(api, panel_id, target)
    except AbroadModePanelError:
        if target is None:
            raise
        # Домашний сквад мог быть удалён в панели — лучше «без сквада», чем застрять на Abroad.
        logger.warning('Home squad rejected by panel, detaching', subscription_id=subscription.id, home_squad=target)
        await _write_squad(api, panel_id, None)
    subscription.abroad_mode = False
    subscription.abroad_home_squad_uuid = None
    await db.commit()
    logger.info('Abroad mode off', subscription_id=subscription.id, restored_squad=target)
    return AbroadModeState(abroad=False, changed=True)
