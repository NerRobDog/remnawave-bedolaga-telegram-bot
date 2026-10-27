"""CRUD recovery-кодов: замена набора, счётчики, атомарное одноразовое списание."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.database.crud.user_recovery_code import (
    consume_recovery_code,
    recovery_code_counts,
    replace_recovery_codes,
)
from app.database.models import User, UserRecoveryCode
from tests.fixtures.sqlite_memory import memory_session


pytestmark = pytest.mark.asyncio

HASH_A, HASH_B, HASH_C = 'a' * 64, 'b' * 64, 'c' * 64


async def _users(db, count: int = 2) -> list[int]:
    users = [User(telegram_id=None, username=f'u{i}', first_name=f'U{i}', status='active') for i in range(count)]
    db.add_all(users)
    await db.commit()
    return [user.id for user in users]


async def test_replace_stores_hashes_only_and_counts_them(monkeypatch) -> None:
    async with memory_session(monkeypatch, [User.__table__, UserRecoveryCode.__table__]) as db:
        (user_id,) = await _users(db, 1)

        await replace_recovery_codes(db, user_id, [HASH_A, HASH_B])
        await db.commit()
        rows = (await db.execute(select(UserRecoveryCode))).scalars().all()
        counts = await recovery_code_counts(db, user_id)

    assert sorted(row.code_hash for row in rows) == [HASH_A, HASH_B]
    assert all(row.user_id == user_id and row.used_at is None for row in rows)
    assert counts == (2, 2)


async def test_replace_wipes_the_old_set_including_spent_codes(monkeypatch) -> None:
    async with memory_session(monkeypatch, [User.__table__, UserRecoveryCode.__table__]) as db:
        first, second = await _users(db)
        await replace_recovery_codes(db, first, [HASH_A, HASH_B])
        await replace_recovery_codes(db, second, [HASH_C])
        await db.commit()
        await consume_recovery_code(db, HASH_A)
        await db.commit()

        await replace_recovery_codes(db, first, ['d' * 64])
        await db.commit()
        old_code_still_works = await consume_recovery_code(db, HASH_B)
        first_counts = await recovery_code_counts(db, first)
        second_counts = await recovery_code_counts(db, second)

    assert old_code_still_works is None
    assert first_counts == (1, 1)
    assert second_counts == (1, 1), 'набор другого пользователя не трогается'


async def test_consume_returns_the_owner_once_and_marks_the_code_spent(monkeypatch) -> None:
    async with memory_session(monkeypatch, [User.__table__, UserRecoveryCode.__table__]) as db:
        (user_id,) = await _users(db, 1)
        await replace_recovery_codes(db, user_id, [HASH_A, HASH_B])
        await db.commit()

        first = await consume_recovery_code(db, HASH_A)
        await db.commit()
        again = await consume_recovery_code(db, HASH_A)
        counts = await recovery_code_counts(db, user_id)
        spent = (await db.execute(select(UserRecoveryCode).where(UserRecoveryCode.code_hash == HASH_A))).scalar_one()

    assert first == user_id
    assert again is None
    assert counts == (1, 2)
    assert spent.used_at is not None


async def test_consume_of_an_unknown_hash_is_none(monkeypatch) -> None:
    async with memory_session(monkeypatch, [User.__table__, UserRecoveryCode.__table__]) as db:
        (user_id,) = await _users(db, 1)
        await replace_recovery_codes(db, user_id, [HASH_A])
        await db.commit()

        assert await consume_recovery_code(db, HASH_C) is None
        assert await recovery_code_counts(db, user_id) == (1, 1)


async def test_two_concurrent_logins_with_one_code_yield_exactly_one_winner(monkeypatch) -> None:
    async with memory_session(monkeypatch, [User.__table__, UserRecoveryCode.__table__]) as db:
        (user_id,) = await _users(db, 1)
        await replace_recovery_codes(db, user_id, [HASH_A])
        await db.commit()
        maker = async_sessionmaker(db.bind, expire_on_commit=False, autoflush=False)

        async def attempt() -> int | None:
            async with maker() as session:
                won = await consume_recovery_code(session, HASH_A)
                await session.commit()
                return won

        results = await asyncio.gather(*(attempt() for _ in range(8)))

    assert sorted(result for result in results if result is not None) == [user_id]
    assert results.count(None) == 7
