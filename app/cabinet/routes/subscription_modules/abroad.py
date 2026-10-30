"""Cabinet API: переключатель «Я в РФ / Я за границей».

GET  /subscription/abroad-mode
POST /subscription/abroad-mode  {"enabled": true|false}
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import User
from app.services import abroad_mode_service as abroad
from app.services.subscription_service import SubscriptionService

from ...dependencies import get_cabinet_db, get_current_cabinet_user
from .helpers import resolve_subscription


logger = structlog.get_logger(__name__)

router = APIRouter()


class AbroadModeRequest(BaseModel):
    enabled: bool


class AbroadModeResponse(BaseModel):
    available: bool
    abroad: bool = False
    changed: bool = False


_ERROR_STATUS = {
    abroad.AbroadModeDisabled: status.HTTP_403_FORBIDDEN,
    abroad.AbroadModeUnavailable: status.HTTP_409_CONFLICT,
    abroad.AbroadModePanelError: status.HTTP_502_BAD_GATEWAY,
}


async def _target_subscription(db: AsyncSession, user: User, subscription_id: int | None):
    from app.database.crud.user import get_user_by_id

    fresh_user = await get_user_by_id(db, user.id)
    if not fresh_user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='User not found')
    subscription = await resolve_subscription(db, fresh_user, subscription_id)
    if not subscription:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Subscription not found')
    return subscription


def _http_error(error: abroad.AbroadModeError) -> HTTPException:
    return HTTPException(
        status_code=_ERROR_STATUS.get(type(error), status.HTTP_400_BAD_REQUEST),
        detail={'code': error.code, 'message': str(error)},
    )


@router.get('/abroad-mode', response_model=AbroadModeResponse)
async def get_abroad_mode(
    subscription_id: int | None = Query(None, description='Subscription ID for multi-tariff'),
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> AbroadModeResponse:
    if not abroad.is_available():
        return AbroadModeResponse(available=False)
    subscription = await _target_subscription(db, user, subscription_id)
    if not subscription.is_active or not subscription.remnawave_id:
        return AbroadModeResponse(available=False)
    # Без обращения к панели: GET дёргается при каждом открытии главной.
    return AbroadModeResponse(available=True, abroad=bool(subscription.abroad_mode))


@router.post('/abroad-mode', response_model=AbroadModeResponse)
async def set_abroad_mode(
    request: AbroadModeRequest,
    subscription_id: int | None = Query(None, description='Subscription ID for multi-tariff'),
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> AbroadModeResponse:
    if not abroad.is_available():
        raise _http_error(abroad.AbroadModeDisabled('abroad mode is disabled'))
    subscription = await _target_subscription(db, user, subscription_id)
    if not subscription.is_active:
        raise _http_error(abroad.AbroadModeUnavailable('subscription is not active'))
    try:
        async with SubscriptionService().get_api_client() as api:
            state = await abroad.set_abroad_mode(db, api, subscription, request.enabled)
    except abroad.AbroadModeError as error:
        logger.warning('Abroad mode switch failed', user_id=user.id, code=error.code, error=str(error))
        raise _http_error(error) from error
    return AbroadModeResponse(available=True, abroad=state.abroad, changed=state.changed)
