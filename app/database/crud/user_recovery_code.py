"""CRUD для ``user_recovery_codes`` (recovery-коды входа для аккаунта без почты и Telegram).

Здесь только хэши: открытый код живёт в ответе один раз и в БД не попадает.
Функции не делают commit — граница транзакции у вызывающего (выпуск набора идёт в
одной транзакции с созданием аккаунта, списание коммитится сразу, до любых проверок).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import UserRecoveryCode


async def replace_recovery_codes(db: AsyncSession, user_id: int, code_hashes: Sequence[str]) -> None:
    """Заменить весь набор пользователя новым (старые, в том числе потраченные, уничтожаются).

    Вызывающий держит ``lock_user_row``: две параллельные перевыдачи иначе перемешали бы наборы.
    """
    await db.execute(delete(UserRecoveryCode).where(UserRecoveryCode.user_id == user_id))
    db.add_all(UserRecoveryCode(user_id=user_id, code_hash=code_hash) for code_hash in code_hashes)
    await db.flush()


async def recovery_code_counts(db: AsyncSession, user_id: int) -> tuple[int, int]:
    """``(осталось неиспользованных, всего в наборе)``."""
    row = (
        await db.execute(
            select(
                func.count(UserRecoveryCode.id),
                func.count(UserRecoveryCode.id).filter(UserRecoveryCode.used_at.is_(None)),
            ).where(UserRecoveryCode.user_id == user_id)
        )
    ).one()
    total, remaining = int(row[0]), int(row[1])
    return remaining, total


async def consume_recovery_code(db: AsyncSession, code_hash: str) -> int | None:
    """Списать код и вернуть id владельца; ``None`` — кода нет или он уже потрачен.

    Один оператор ``UPDATE ... WHERE used_at IS NULL RETURNING``: проверка и списание
    не разделены, поэтому из двух параллельных входов одним кодом выигрывает ровно один
    (второй, дождавшись строки, не находит неиспользованного кода). Чтение «потом запись»
    здесь недопустимо.
    """
    result = await db.execute(
        update(UserRecoveryCode)
        .where(UserRecoveryCode.code_hash == code_hash, UserRecoveryCode.used_at.is_(None))
        .values(used_at=datetime.now(UTC))
        .returning(UserRecoveryCode.user_id)
    )
    return result.scalar_one_or_none()
