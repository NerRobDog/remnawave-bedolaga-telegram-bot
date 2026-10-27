"""Схемы passkey (WebAuthn) кабинета."""

import json
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator


#: Потолок на сериализованный ответ аутентификатора. Настоящая attestation с цепочкой
#: сертификатов — единицы килобайт; больше — не WebAuthn, парсить незачем.
MAX_CREDENTIAL_BYTES = 65_536
MAX_PASSKEY_NAME_LENGTH = 100


class PasskeyStatusResponse(BaseModel):
    enabled: bool


class PasskeyOptionsResponse(BaseModel):
    challenge_id: str
    options: dict[str, Any]


class PasskeyVerifyRequest(BaseModel):
    challenge_id: str = Field(..., min_length=16, max_length=128)
    credential: Any = Field(..., description='PublicKeyCredential.toJSON() из navigator.credentials.create()/get()')

    @field_validator('credential')
    @classmethod
    def _credential_is_a_bounded_object(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            raise ValueError('credential must be an object')
        if len(json.dumps(value, separators=(',', ':'), default=str)) > MAX_CREDENTIAL_BYTES:
            raise ValueError('credential is too large')
        return value


def _clean_name(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        raise ValueError('name must not be blank')
    return value


class PasskeyRegisterVerifyRequest(PasskeyVerifyRequest):
    name: str | None = Field(None, max_length=MAX_PASSKEY_NAME_LENGTH, description='Название ключа, например «MacBook»')

    _strip_name = field_validator('name')(_clean_name)


class PasskeyRenameRequest(BaseModel):
    name: str = Field(..., max_length=MAX_PASSKEY_NAME_LENGTH)

    _strip_name = field_validator('name')(_clean_name)


class PasskeyResponse(BaseModel):
    """Ключ без ключевого материала: ни credential id, ни публичного ключа, ни счётчика."""

    id: int
    name: str
    device_type: str
    backed_up: bool
    transports: list[str]
    created_at: datetime | None
    last_used_at: datetime | None


class PasskeyListResponse(BaseModel):
    passkeys: list[PasskeyResponse]
    limit: int


class PasskeyDeleteResponse(BaseModel):
    success: bool
