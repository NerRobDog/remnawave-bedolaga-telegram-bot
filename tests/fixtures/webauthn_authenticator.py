"""Программный WebAuthn-аутентификатор для тестов passkey.

Настоящий EC P-256 ключ (``cryptography``), вручную собранные clientDataJSON,
authenticatorData, attestationObject (fmt=none, CBOR через ``cbor2``) и подпись
утверждения. Серверная проверка проходит через настоящий ``py_webauthn`` —
никаких подмен библиотеки. Параметры конструктора и методов нужны, чтобы
собирать заведомо неверные ответы: чужой origin, чужой rpId, без user
verification, с чужим challenge.

Что делает браузер, а не аутентификатор (clientDataJSON, JSON-обёртка
PublicKeyCredential), собирается здесь же: тесту нужен ответ в том виде, в каком
его присылает ``navigator.credentials.create()/get()`` после ``toJSON()``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import struct

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec


FLAG_UP = 0x01
FLAG_UV = 0x04
FLAG_BE = 0x08
FLAG_BS = 0x10
FLAG_AT = 0x40

AAGUID = bytes(16)


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('ascii')


def unb64u(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + '=' * (-len(value) % 4))


class SoftwareAuthenticator:
    """Один резидентный credential на одном устройстве."""

    def __init__(
        self,
        *,
        rp_id: str,
        origin: str,
        credential_id: bytes | None = None,
        sign_count: int = 0,
        backed_up: bool = False,
    ) -> None:
        self.rp_id = rp_id
        self.origin = origin
        self.credential_id = credential_id or os.urandom(32)
        self.sign_count = sign_count
        self.backed_up = backed_up
        self.user_handle: bytes | None = None
        self._key = ec.generate_private_key(ec.SECP256R1())

    # -- registration ------------------------------------------------------

    def _cose_public_key(self) -> bytes:
        numbers = self._key.public_key().public_numbers()
        return cbor2.dumps(
            {
                1: 2,  # kty: EC2
                3: -7,  # alg: ES256
                -1: 1,  # crv: P-256
                -2: numbers.x.to_bytes(32, 'big'),
                -3: numbers.y.to_bytes(32, 'big'),
            }
        )

    def _flags(self, *, user_verified: bool, attested: bool) -> int:
        flags = FLAG_UP
        if user_verified:
            flags |= FLAG_UV
        if self.backed_up:
            flags |= FLAG_BE | FLAG_BS
        if attested:
            flags |= FLAG_AT
        return flags

    def create(
        self,
        options: dict,
        *,
        origin: str | None = None,
        rp_id: str | None = None,
        challenge: str | None = None,
        user_verified: bool = True,
        user_present: bool = True,
        client_data_type: str = 'webauthn.create',
        cross_origin: bool | None = None,
        raw_id: bytes | None = None,
        transports: list[str] | None = None,
    ) -> dict:
        """Ответ ``navigator.credentials.create()`` на options от сервера."""
        challenge_b64 = challenge if challenge is not None else options['challenge']
        self.user_handle = unb64u(options['user']['id'])
        client_data = {
            'type': client_data_type,
            'challenge': challenge_b64,
            'origin': origin if origin is not None else self.origin,
        }
        if cross_origin is not None:
            client_data['crossOrigin'] = cross_origin
        client_data_json = json.dumps(client_data).encode()

        rp_hash = hashlib.sha256((rp_id if rp_id is not None else self.rp_id).encode()).digest()
        flags = self._flags(user_verified=user_verified, attested=True)
        if not user_present:
            flags &= ~FLAG_UP
        auth_data = (
            rp_hash
            + bytes([flags])
            + struct.pack('>I', self.sign_count)
            + AAGUID
            + struct.pack('>H', len(self.credential_id))
            + self.credential_id
            + self._cose_public_key()
        )
        attestation_object = cbor2.dumps({'fmt': 'none', 'attStmt': {}, 'authData': auth_data})

        wire_id = raw_id if raw_id is not None else self.credential_id
        return {
            'id': b64u(wire_id),
            'rawId': b64u(wire_id),
            'type': 'public-key',
            'authenticatorAttachment': 'platform',
            'clientExtensionResults': {},
            'response': {
                'clientDataJSON': b64u(client_data_json),
                'attestationObject': b64u(attestation_object),
                'transports': transports if transports is not None else ['internal'],
            },
        }

    # -- authentication ----------------------------------------------------

    def get(
        self,
        options: dict,
        *,
        origin: str | None = None,
        rp_id: str | None = None,
        challenge: str | None = None,
        user_verified: bool = True,
        sign_count: int | None = None,
        user_handle: bytes | None = None,
        omit_user_handle: bool = False,
        client_data_type: str = 'webauthn.get',
        signing_key: ec.EllipticCurvePrivateKey | None = None,
    ) -> dict:
        """Ответ ``navigator.credentials.get()`` на options от сервера.

        Без явного ``sign_count`` счётчик растёт на единицу, как у настоящего
        аутентификатора; ``sign_count=0`` имитирует устройство без счётчика.
        """
        if sign_count is None:
            self.sign_count += 1
            sign_count = self.sign_count
        else:
            self.sign_count = sign_count

        challenge_b64 = challenge if challenge is not None else options['challenge']
        client_data_json = json.dumps(
            {
                'type': client_data_type,
                'challenge': challenge_b64,
                'origin': origin if origin is not None else self.origin,
            }
        ).encode()

        rp_hash = hashlib.sha256((rp_id if rp_id is not None else self.rp_id).encode()).digest()
        flags = self._flags(user_verified=user_verified, attested=False)
        auth_data = rp_hash + bytes([flags]) + struct.pack('>I', sign_count)

        key = signing_key or self._key
        signature = key.sign(auth_data + hashlib.sha256(client_data_json).digest(), ec.ECDSA(hashes.SHA256()))

        response = {
            'clientDataJSON': b64u(client_data_json),
            'authenticatorData': b64u(auth_data),
            'signature': b64u(signature),
        }
        handle = user_handle if user_handle is not None else self.user_handle
        if handle is not None and not omit_user_handle:
            response['userHandle'] = b64u(handle)

        return {
            'id': b64u(self.credential_id),
            'rawId': b64u(self.credential_id),
            'type': 'public-key',
            'authenticatorAttachment': 'platform',
            'clientExtensionResults': {},
            'response': response,
        }

    def foreign_key(self) -> ec.EllipticCurvePrivateKey:
        """Ключ злоумышленника: чужая подпись под тем же credential id."""
        return ec.generate_private_key(ec.SECP256R1())
