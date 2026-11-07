"""Выдача триала с учётом лёгкого режима: журнал личностей и бюджет в одном месте.

Кабинет зовёт ``check_trial_availability`` для ответа «доступен ли триал» (ничего не меняет) и
``claim_trial`` перед созданием подписки. ``claim_trial`` занимает бюджет (только для ``lite``) и
личность в журнале; если подписку создать не удалось, вызывающий зовёт ``TrialClaim.release``.

Без ``TRIAL_LITE_ENABLED`` обе функции ничего не делают: ни Redis, ни журнал не трогаются, и
выдача триала идёт как раньше.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import structlog
from sqlalchemy import delete

from app.config import settings
from app.database.models import TrialIdentityLedger
from app.services.lite_trial_budget import LiteTrialReservation, peek_lite_trial, reserve_lite_trial
from app.services.trial_eligibility import LiteTrialReason, LiteTrialRefused, TrialMode
from app.services.trial_identity import claim_identities, ensure_identities_free


logger = structlog.get_logger(__name__)


@dataclass
class TrialClaim:
    """Что занято под выдачу триала и должно быть возвращено, если выдача сорвалась."""

    mode: TrialMode
    reservation: LiteTrialReservation | None = None
    identity_hashes: list[str] = field(default_factory=list)

    async def release(self, db: Any) -> None:
        """Откатывает занятое. Сессию откатывает первой: после сбоя она может быть в ошибке.

        После вызова ORM-объекты сессии протухают — вызывающий не должен к ним обращаться.
        """
        try:
            await db.rollback()
            if self.identity_hashes:
                await db.execute(
                    delete(TrialIdentityLedger).where(TrialIdentityLedger.identity_hash.in_(self.identity_hashes))
                )
                await db.commit()
        except Exception:
            logger.warning('Failed to release trial identities', exc_info=True)
            await db.rollback()
        if self.reservation is not None:
            await self.reservation.release()


async def check_trial_availability(
    db: Any, user: Any, mode: TrialMode, client_ip: str | None
) -> LiteTrialReason | None:
    """Причина, по которой триал этого режима сейчас не выдать, или ``None``. Ничего не занимает."""
    if not settings.TRIAL_LITE_ENABLED:
        return None
    try:
        await ensure_identities_free(db, user)
    except LiteTrialRefused as refused:
        return refused.reason
    if mode == TrialMode.LITE:
        return await peek_lite_trial(client_ip)
    return None


async def claim_trial(db: Any, user: Any, mode: TrialMode, client_ip: str | None) -> TrialClaim:
    """Занимает бюджет (для ``lite``) и личность в журнале или бросает ``LiteTrialRefused``."""
    claim = TrialClaim(mode=mode)
    if not settings.TRIAL_LITE_ENABLED:
        return claim

    await ensure_identities_free(db, user)
    if mode == TrialMode.LITE:
        claim.reservation = await reserve_lite_trial(client_ip)
    try:
        claim.identity_hashes = await claim_identities(db, user)
    except BaseException:
        if claim.reservation is not None:
            await claim.reservation.release()
        raise
    return claim
