"""CRUD операции для платежей Antarctic Wallet."""

from datetime import UTC, datetime
from decimal import Decimal

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import AntarcticPayment


logger = structlog.get_logger(__name__)


async def create_antarctic_payment(
    db: AsyncSession,
    *,
    user_id: int | None,
    token: str,
    amount_kopeks: int,
    amount_usd_cents: int,
    usd_rub_rate: Decimal,
    description: str | None,
    expires_at: datetime | None,
    metadata_json: dict | None,
) -> AntarcticPayment:
    """Создаёт запись о платеже Antarctic Wallet."""
    payment = AntarcticPayment(
        user_id=user_id,
        token=token,
        amount_kopeks=amount_kopeks,
        amount_usd_cents=amount_usd_cents,
        usd_rub_rate=usd_rub_rate,
        description=description,
        expires_at=expires_at,
        metadata_json=metadata_json,
        status='pending',
        is_paid=False,
    )
    db.add(payment)
    await db.commit()
    await db.refresh(payment)
    logger.info('Создан платеж Antarctic Wallet', payment_id=payment.id, user_id=user_id)
    return payment


async def get_antarctic_payment_by_token(db: AsyncSession, token: str) -> AntarcticPayment | None:
    result = await db.execute(select(AntarcticPayment).where(AntarcticPayment.token == token))
    return result.scalar_one_or_none()


async def get_antarctic_payment_by_id(db: AsyncSession, payment_id: int) -> AntarcticPayment | None:
    result = await db.execute(select(AntarcticPayment).where(AntarcticPayment.id == payment_id))
    return result.scalar_one_or_none()


async def get_antarctic_payment_by_id_for_update(db: AsyncSession, payment_id: int) -> AntarcticPayment | None:
    """Получает платёж с блокировкой FOR UPDATE."""
    result = await db.execute(
        select(AntarcticPayment)
        .where(AntarcticPayment.id == payment_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def get_antarctic_payment_by_operation_id(db: AsyncSession, operation_id: str) -> AntarcticPayment | None:
    result = await db.execute(select(AntarcticPayment).where(AntarcticPayment.operation_id == operation_id))
    return result.scalar_one_or_none()


async def link_antarctic_payment_to_transaction(
    db: AsyncSession,
    payment: AntarcticPayment,
    transaction_id: int,
) -> AntarcticPayment:
    """Связывает платёж с транзакцией (без commit: вызывается внутри зачисления)."""
    payment.transaction_id = transaction_id
    payment.updated_at = datetime.now(UTC)
    await db.flush()
    await db.refresh(payment)
    return payment
