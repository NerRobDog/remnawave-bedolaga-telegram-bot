"""CRUD для ``user_passkeys`` (passkey / WebAuthn credentials кабинета)."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import User, UserPasskey


async def lock_user_row(db: AsyncSession, user_id: int) -> None:
    """Сериализовать конкурентные изменения набора ключей одного пользователя.

    Лимит числа ключей и правило «нельзя удалить последний способ входа» —
    «посчитал, потом записал»; без блокировки два параллельных запроса оба видят
    допустимое состояние и вместе нарушают его.
    """
    await db.execute(select(User.id).where(User.id == user_id).with_for_update())


async def list_user_passkeys(db: AsyncSession, user_id: int) -> list[UserPasskey]:
    result = await db.execute(select(UserPasskey).where(UserPasskey.user_id == user_id).order_by(UserPasskey.id))
    return list(result.scalars())


async def count_user_passkeys(db: AsyncSession, user_id: int, *, excluding_id: int | None = None) -> int:
    query = select(func.count()).select_from(UserPasskey).where(UserPasskey.user_id == user_id)
    if excluding_id is not None:
        query = query.where(UserPasskey.id != excluding_id)
    return int((await db.execute(query)).scalar_one())


async def get_user_passkey(db: AsyncSession, user_id: int, passkey_id: int) -> UserPasskey | None:
    """Ключ по id — только если он принадлежит пользователю (иначе None, как будто его нет)."""
    result = await db.execute(select(UserPasskey).where(UserPasskey.id == passkey_id, UserPasskey.user_id == user_id))
    return result.scalar_one_or_none()


async def get_passkey_by_credential_id(db: AsyncSession, credential_id: bytes) -> UserPasskey | None:
    result = await db.execute(select(UserPasskey).where(UserPasskey.credential_id == credential_id))
    return result.scalar_one_or_none()


async def record_assertion(
    db: AsyncSession,
    passkey: UserPasskey,
    *,
    seen_sign_count: int,
    new_sign_count: int,
    backed_up: bool,
) -> bool:
    """Записать успешный вход: счётчик, флаг резервной копии, время.

    Сравнение-и-замена по счётчику, который был прочитан перед проверкой: если за это
    время тот же ключ уже вошёл (параллельный запрос или клон), обновление не
    затрагивает ни одной строки и возвращается False — вход надо отклонить.
    """
    result = await db.execute(
        update(UserPasskey)
        .where(UserPasskey.id == passkey.id, UserPasskey.sign_count == seen_sign_count)
        .values(sign_count=new_sign_count, backed_up=backed_up, last_used_at=datetime.now(UTC))
    )
    return result.rowcount == 1
