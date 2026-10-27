"""Стенд для HTTP-тестов passkey: настоящее FastAPI-приложение, JWT и in-memory БД.

Зависимость ``get_current_cabinet_user`` не подменяется — запросы с токеном идут
через неё же, как в проде; подменена только выдача сессии БД. Redis — в памяти
(``fake_redis``), поэтому челленджи и rate-limit работают настоящим кодом.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.cabinet.auth import create_access_token
from app.cabinet.dependencies import get_cabinet_db
from app.cabinet.routes import passkey as passkey_routes
from app.database.models import (
    AdminRole,
    CabinetRefreshToken,
    PromoGroup,
    Subscription,
    SystemSetting,
    Tariff,
    User,
    UserPasskey,
    UserPromoGroup,
    UserRole,
    UserStatus,
)
from tests.cabinet.conftest import PASSKEY_ORIGIN, PASSKEY_RP_ID
from tests.fixtures.fake_redis import FakeRedis
from tests.fixtures.sqlite_memory import memory_session
from tests.fixtures.webauthn_authenticator import SoftwareAuthenticator


TABLES = (
    User.__table__,
    UserPasskey.__table__,
    CabinetRefreshToken.__table__,
    Subscription.__table__,
    Tariff.__table__,
    UserPromoGroup.__table__,
    PromoGroup.__table__,
    UserRole.__table__,
    AdminRole.__table__,
    SystemSetting.__table__,
)


def new_authenticator(**kwargs) -> SoftwareAuthenticator:
    return SoftwareAuthenticator(rp_id=PASSKEY_RP_ID, origin=PASSKEY_ORIGIN, **kwargs)


@dataclass
class PasskeyHttp:
    client: AsyncClient
    db: AsyncSession
    redis: FakeRedis
    _counter: int = field(default=0)

    # -- пользователи ------------------------------------------------------

    async def make_user(self, **fields) -> User:
        self._counter += 1
        values = dict(
            telegram_id=None,
            username=f'user{self._counter}',
            first_name=f'User {self._counter}',
            status=UserStatus.ACTIVE.value,
            language='ru',
        )
        values.update(fields)
        user = User(**values)
        self.db.add(user)
        await self.db.commit()
        await self.db.refresh(user)
        # Сессия общая с приложением: rollback в обработчике протушил бы этот объект.
        # Отсоединённый, он остаётся читаемым, а обработчик загрузит свою копию.
        self.db.expunge(user)
        return user

    @staticmethod
    def auth(user: User) -> dict[str, str]:
        return {'Authorization': f'Bearer {create_access_token(user.id, user.telegram_id)}'}

    # -- запросы -----------------------------------------------------------

    async def post(self, path: str, user: User | None = None, **json) -> Response:
        return await self.client.post(path, json=json, headers=self.auth(user) if user else None)

    async def register_options(self, user: User) -> Response:
        return await self.post('/cabinet/passkey/register/options', user)

    async def register_verify(self, user: User, challenge_id: str, credential: dict, **extra) -> Response:
        return await self.post(
            '/cabinet/passkey/register/verify', user, challenge_id=challenge_id, credential=credential, **extra
        )

    async def register(
        self, user: User, authenticator: SoftwareAuthenticator | None = None, *, name: str | None = None
    ) -> tuple[SoftwareAuthenticator, Response]:
        """Полный круг: options → программный аутентификатор → verify."""
        authenticator = authenticator or new_authenticator()
        options = (await self.register_options(user)).json()
        credential = authenticator.create(options['options'])
        extra = {'name': name} if name is not None else {}
        return authenticator, await self.register_verify(user, options['challenge_id'], credential, **extra)

    async def login_options(self) -> Response:
        return await self.post('/cabinet/auth/passkey/login/options')

    async def login_verify(self, challenge_id: str, credential: dict) -> Response:
        return await self.post('/cabinet/auth/passkey/login/verify', challenge_id=challenge_id, credential=credential)

    async def login(self, authenticator: SoftwareAuthenticator, **get_kwargs) -> Response:
        options = (await self.login_options()).json()
        return await self.login_verify(options['challenge_id'], authenticator.get(options['options'], **get_kwargs))

    # -- БД ----------------------------------------------------------------

    async def passkeys(self, user: User | None = None) -> list[UserPasskey]:
        query = select(UserPasskey).order_by(UserPasskey.id)
        if user is not None:
            query = query.where(UserPasskey.user_id == user.id)
        result = await self.db.execute(query.execution_options(populate_existing=True))
        return list(result.scalars())

    async def refresh_tokens(self) -> list[CabinetRefreshToken]:
        query = select(CabinetRefreshToken).execution_options(populate_existing=True)
        return list((await self.db.execute(query)).scalars())


@contextlib.asynccontextmanager
async def open_harness(monkeypatch, redis: FakeRedis) -> AsyncIterator[PasskeyHttp]:
    async with memory_session(monkeypatch, TABLES) as db:
        app = FastAPI()
        app.include_router(passkey_routes.auth_router, prefix='/cabinet')
        app.include_router(passkey_routes.router, prefix='/cabinet')

        async def _db() -> AsyncIterator[AsyncSession]:
            yield db

        app.dependency_overrides[get_cabinet_db] = _db
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://cabinet') as client:
            yield PasskeyHttp(client=client, db=db, redis=redis)
