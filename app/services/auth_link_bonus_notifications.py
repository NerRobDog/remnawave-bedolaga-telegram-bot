"""Всё, что происходит ПОСЛЕ коммита выдачи бонуса за привязку способа входа.

Дни уже в базе, привязка уже закоммичена. Здесь три действия, и ни одно не бросает: сбой любого
не должен ни откатывать выданное, ни ронять ответ на привязку.

* синхронизация нового срока с панелью (как после наград за реферала: расхождение с панелью чинится
  следующей синхронизацией, а потеря начисленных дней — нет);
* событие кабинету по WebSocket (``auth_link_bonus.granted``) — для всех, кто сейчас открыт в кабинете;
* сообщение через ``notification_delivery_service``: Telegram, если он у человека есть, иначе
  подтверждённая почта. Текст — ключ локализации ``AUTH_LINK_BONUS_GRANTED``, письмо — тип
  ``auth_link_bonus`` в редакторе email-шаблонов.
"""

from __future__ import annotations

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import Subscription, User
from app.services.auth_link_bonus_service import LinkBonusGrant
from app.services.notification_types import NotificationType


logger = structlog.get_logger(__name__)

_DATE_FORMAT = '%d.%m.%Y %H:%M'


async def deliver_link_bonus(db: AsyncSession, user: User, grant: LinkBonusGrant) -> None:
    """Синхронизация с панелью и уведомления о выданном бонусе. Никогда не бросает."""
    user_id = grant.user_id
    # Берём всё нужное из пользователя ДО похода в панель: её сбой откатывает сессию,
    # и ORM-объект протухает (в async-сессии это MissingGreenlet).
    try:
        language = (user.language or settings.DEFAULT_LANGUAGE) or 'ru'
        telegram_id = user.telegram_id
    except Exception:
        logger.exception('Link bonus: could not read the user, notification skipped', user_id=user_id)
        return

    await _sync_panel(db, grant)
    await _notify_cabinet(grant)
    await _notify_user(user, language, telegram_id, grant)


async def _sync_panel(db: AsyncSession, grant: LinkBonusGrant) -> None:
    try:
        from app.services.subscription_service import SubscriptionService

        subscription = (
            (
                await db.execute(
                    select(Subscription)
                    .where(Subscription.id == grant.subscription_id)
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .first()
        )
        if subscription is None:
            return
        await SubscriptionService().sync_remnawave_user(db, subscription)
    except Exception:
        logger.exception(
            'Link bonus: panel sync failed, the days stay in the database',
            user_id=grant.user_id,
            subscription_id=grant.subscription_id,
        )


async def _notify_cabinet(grant: LinkBonusGrant) -> None:
    try:
        from app.cabinet.routes.websocket import notify_user_link_bonus_granted

        await notify_user_link_bonus_granted(grant.user_id, grant.days, grant.new_end_date)
    except Exception:
        logger.warning('Link bonus: cabinet event not delivered', user_id=grant.user_id, exc_info=True)


async def _notify_user(user: User, language: str, telegram_id: int | None, grant: LinkBonusGrant) -> None:
    bot = None
    try:
        from app.bot_factory import create_bot
        from app.localization.texts import get_texts
        from app.services.notification_delivery_service import notification_delivery_service
        from app.utils.timezone import format_local_datetime

        until = format_local_datetime(grant.new_end_date, _DATE_FORMAT)
        template = get_texts(language).get('AUTH_LINK_BONUS_GRANTED')
        message = template.format(days=grant.days, until=until) if template else None

        bot = create_bot(token=settings.BOT_TOKEN) if telegram_id and settings.BOT_TOKEN else None
        await notification_delivery_service.send_notification(
            user=user,
            notification_type=NotificationType.AUTH_LINK_BONUS,
            context={'days': grant.days, 'until': until},
            bot=bot,
            telegram_message=message,
            # Событие кабинету уже отправлено выше одно для всех; второго от почтового канала не нужно.
            use_websocket=False,
        )
    except Exception:
        logger.warning('Link bonus: user notification failed', user_id=grant.user_id, exc_info=True)
    finally:
        if bot is not None:
            try:
                await bot.session.close()
            except Exception as error:
                logger.debug('Не удалось закрыть сессию бота после уведомления', error=str(error))
