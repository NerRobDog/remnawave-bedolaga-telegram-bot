"""Вопросы клиенту на «голое» первое сообщение обращения.

``is_bare`` портирует эвристику кабинета ``src/utils/ticketQuality.ts``
(``checkTicketMessage``): те же пороги и тот же набор слов жалобы.
``pick_questions`` выбирает из фиксированного набора только то, чего ещё нет
в тексте и данных клиента. Ничего не генерируется моделью.
"""

from __future__ import annotations

import re

from app.localization.texts import get_texts
from app.services.support_assist.flags import Flag
from app.services.support_assist.snapshot import Snapshot


QUESTION_IDS = ('device', 'app', 'since_when', 'network', 'scope', 'sites', 'screenshot')

MIN_TICKET_MESSAGE_LENGTH = 120
MIN_TICKET_MESSAGE_WORDS = 15
MIN_TICKET_DETAIL_WORDS = 6

# Слова самой жалобы; всё остальное считается деталью (как в кабинете).
COMPLAINT_WORDS: frozenset[str] = frozenset(
    {
        # ru
        'не',
        'нет',
        'ни',
        'работает',
        'работают',
        'работал',
        'работала',
        'работать',
        'работа',
        'пашет',
        'фурычит',
        'функционирует',
        'подключается',
        'подключиться',
        'подключение',
        'подключает',
        'коннектится',
        'открывается',
        'грузится',
        'грузит',
        'идёт',
        'идет',
        'ловит',
        'сломался',
        'сломалось',
        'лежит',
        'упал',
        'упала',
        'глючит',
        'висит',
        'vpn',
        'впн',
        'вpн',
        'ви',
        'пи',
        'эн',
        'интернет',
        'сервис',
        'сайт',
        'помогите',
        'помоги',
        'помощь',
        'пожалуйста',
        'плиз',
        'срочно',
        'проблема',
        'проблемы',
        'опять',
        'снова',
        'всё',
        'все',
        'вообще',
        'совсем',
        'у',
        'меня',
        'мой',
        'моя',
        'и',
        'а',
        'в',
        'это',
        'как',
        'почему',
        'что',
        'то',
        'же',
        'ли',
        # en
        'not',
        'no',
        'doesn',
        'don',
        'isn',
        'aren',
        'didn',
        'wasn',
        't',
        's',
        'work',
        'works',
        'working',
        'worked',
        'connect',
        'connects',
        'connected',
        'connecting',
        'connection',
        'broken',
        'down',
        'help',
        'please',
        'pls',
        'urgent',
        'problem',
        'issue',
        'again',
        'my',
        'the',
        'is',
        'it',
        'a',
        'i',
        'at',
        'all',
    }
)

_WORD_RE = re.compile(r'[^\W_]+', re.UNICODE)

# Флаги, при которых причина на нашей стороне: клиента не расспрашиваем.
OUR_FAULT = frozenset(
    {
        'sub_expired',
        'desync_status',
        'desync_expire',
        'last_node_down',
        'all_squad_nodes_down',
        'daily_paused',
        'no_subscription',
        'restricted',
    }
)

_APP_NAMES = ('clash mi', 'clashmi', 'happ', 'rabbit hole', 'rabbithole', 'flclash', 'v2ray', 'hiddify')

# (ключевое слово в тексте, подстроки платформы/модели в данных устройства)
_DEVICE_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ('iphone', ('ios', 'iphone')),
    ('айфон', ('ios', 'iphone')),
    ('ipad', ('ios', 'ipad')),
    ('айпад', ('ios', 'ipad')),
    ('android', ('android',)),
    ('андроид', ('android',)),
    ('windows', ('windows',)),
    ('виндовс', ('windows',)),
    ('macbook', ('macos', 'mac')),
    ('макбук', ('macos', 'mac')),
    ('macos', ('macos', 'mac')),
    ('linux', ('linux',)),
    ('телевизор', ('tv',)),
    ('smart tv', ('tv',)),
    ('android tv', ('tv',)),
    ('роутер', ('router',)),
    ('router', ('router',)),
)


def is_bare(text: str | None, has_media: bool) -> bool:
    """True, если первое сообщение не даёт материала для разбора."""
    normalized = re.sub(r'\s+', ' ', text or '').strip()
    if not normalized:
        return bool(has_media)
    words = _WORD_RE.findall(normalized.lower())
    details = {w for w in words if w not in COMPLAINT_WORDS}
    if not details:
        return True
    return (
        len(normalized) < MIN_TICKET_MESSAGE_LENGTH
        or len(words) < MIN_TICKET_MESSAGE_WORDS
        or len(details) < MIN_TICKET_DETAIL_WORDS
    )


def _names_other_device(text: str, s: Snapshot) -> bool:
    known = ' '.join(f'{d.get("platform") or ""} {d.get("device_model") or ""}'.lower() for d in s.devices)
    return any(keyword in text and not any(m in known for m in markers) for keyword, markers in _DEVICE_KEYWORDS)


def pick_questions(text: str, has_media: bool, s: Snapshot, flags: list[Flag]) -> list[str]:
    """Идентификаторы вопросов, которые стоит задать клиенту, в порядке QUESTION_IDS."""
    if any(f.code in OUR_FAULT for f in flags):
        return []
    low = (text or '').lower()
    skip: set[str] = set()
    if s.devices and not _names_other_device(low, s):
        skip.add('device')
    if has_media:
        skip.add('screenshot')
    if any(name in low for name in _APP_NAMES):
        skip.add('app')
    return [q for q in QUESTION_IDS if q not in skip]


def render_questions(ids: list[str], lang: str) -> str:
    """Текст сообщения клиенту: вступление и нумерованные вопросы из локалей."""
    texts = get_texts(lang)
    lines = [texts.t('SUPPORT_ASSIST_Q_INTRO')]
    lines.extend(f'{n}. {texts.t(f"SUPPORT_ASSIST_Q_{qid.upper()}")}' for n, qid in enumerate(ids, 1))
    return '\n'.join(lines)
