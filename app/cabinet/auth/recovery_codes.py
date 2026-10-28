"""Recovery-коды для аккаунта без почты и Telegram. Без FastAPI и без БД.

Код — 80 бит из ``secrets`` (16 символов Crockford base32), человеку показывается
как ``XXXX-XXXX-XXXX-XXXX``. Сервер хранит только HMAC-SHA256 кода под перцем из
секрета кабинета: открытый код нигде не записывается и не логируется, а утечка
таблицы без секрета не позволяет перебирать коды офлайн. Сравнения секрета в Python
нет совсем: строку находит БД по равенству хэша, а подобрать префикс чужого хэша
без секрета нельзя — тайминг ничего не раскрывает.

Ротация ``CABINET_JWT_SECRET`` обнуляет все выданные коды (хэши перестанут совпадать):
после неё владельцам нужно перевыпустить набор.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

from app.cabinet.auth.pepper import pepper_secret


#: Crockford base32: без I, L, O, U — их не путают с 1, 0 и V.
ALPHABET = '0123456789ABCDEFGHJKMNPQRSTVWXYZ'
CODE_LENGTH = 16  # 16 * 5 бит = 80 бит энтропии
GROUP_SIZE = 4
CODES_PER_SET = 8
#: Больше ввода не разбираем: настоящий код вместе с разделителями в разы короче.
MAX_TYPED_LENGTH = 64

_PEPPER_LABEL = b'bedolaga-cabinet-recovery-code:'
_SEPARATORS = str.maketrans('', '', ' -_\t')
#: Crockford: ошибочно набранные символы читаются как похожие цифры.
_LOOKALIKES = str.maketrans({'I': '1', 'L': '1', 'O': '0'})


def generate_code() -> str:
    """Новый код: 16 равновероятных символов алфавита, без разделителей."""
    return ''.join(ALPHABET[secrets.randbelow(len(ALPHABET))] for _ in range(CODE_LENGTH))


def format_code(code: str) -> str:
    """``0123456789ABCDEF`` → ``0123-4567-89AB-CDEF``: так код показывают человеку."""
    return '-'.join(code[i : i + GROUP_SIZE] for i in range(0, len(code), GROUP_SIZE))


def normalize_code(raw: object) -> str | None:
    """Привести введённый человеком код к каноническому виду; ``None`` — это не код."""
    if not isinstance(raw, str) or not raw or len(raw) > MAX_TYPED_LENGTH:
        return None
    code = raw.translate(_SEPARATORS).upper().translate(_LOOKALIKES)
    if len(code) != CODE_LENGTH or not set(code) <= set(ALPHABET):
        return None
    return code


def _pepper() -> bytes:
    return _PEPPER_LABEL + pepper_secret()


def hash_code(code: str) -> str:
    """HMAC-SHA256 канонического кода под перцем; hex, 64 символа."""
    return hmac.new(_pepper(), code.encode('ascii'), hashlib.sha256).hexdigest()


def hash_typed_code(raw: object) -> str | None:
    """Хэш введённого человеком кода; ``None``, если ввод не похож на код."""
    code = normalize_code(raw)
    return hash_code(code) if code is not None else None


def new_code_set(count: int = CODES_PER_SET) -> list[tuple[str, str]]:
    """``count`` различных кодов: пары (код для показа человеку, хэш для БД)."""
    pairs: dict[str, str] = {}
    while len(pairs) < count:
        code = generate_code()
        pairs.setdefault(hash_code(code), format_code(code))
    return [(shown, digest) for digest, shown in pairs.items()]
