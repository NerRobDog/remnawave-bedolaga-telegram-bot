"""Build app/services/support_assist/kb_clients.md from docs/support-kb.

Inputs: docs/support-kb/clients.yaml (live-verified entries) and
docs/support-kb/source/*.json (source/docs-checked labels and menu paths).
Only verified material goes in: live-verified items and labels with
verify=confirmed. Refuted / not_checked labels and menu paths contradicted by a
menu_path_issues entry are dropped (when unsure, drop).

Run: uv run python scripts/build_support_kb.py   (writes the file)
     uv run python scripts/build_support_kb.py --check   (exit 1 if stale)
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parent.parent
KB_DIR = ROOT / 'docs' / 'support-kb'
OUT = ROOT / 'app' / 'services' / 'support_assist' / 'kb_clients.md'

LIVE_APP_KEYS = {'clash-mi'}  # same UI on other platforms: live entry covers the paths
APP_ORDER = ['clash-mi', 'flclashx', 'koala-clash', 'flowvy', 'prizrak-box', 'rabbithole', 'happ']
DISPLAY = {
    'clash-mi': 'Clash Mi',
    'flclashx': 'FlClash X',
    'koala-clash': 'Koala Clash',
    'flowvy': 'Flowvy',
    'prizrak-box': 'Prizrak-Box',
    'rabbithole': 'Rabbit Hole',
    'happ': 'Happ',
}

# Core tasks, in output order: (id, Russian caption, keyword regex over flow / task name).
CATEGORIES: list[tuple[str, str, str]] = [
    ('add', 'добавить подписку', r'\badd\b'),
    ('update', 'обновить подписку', r'\bupdate\b|\bsync\b'),
    ('connect', 'подключить/отключить', r'connect|disconnect|\bstart\b|\bstop\b|\bturn the vpn\b|\bvpn toggle\b'),
    ('mode', 'режим', r'\bmode\b|\brouting\b'),
    ('server', 'выбор сервера', r'\bserver\b|\bnode\b|latency|\bping\b'),
    ('tun', 'системный прокси / TUN', r'\btun\b|system proxy'),
    ('language', 'язык', r'\blanguage\b'),
    ('share', 'поделиться ссылкой', r'\bshare\b|\bcopy\b'),
    ('check', 'проверка сети', r'diagnos|network check'),
]
CAT_IDS = [c[0] for c in CATEGORIES]
CAT_RE = {c[0]: re.compile(c[2], re.IGNORECASE) for c in CATEGORIES}
CAT_CAPTION = {c[0]: c[1] for c in CATEGORIES}

# Paths dropped on top of the generic issue matching, after reading the skeptic notes.
EXTRA_DROPS: list[tuple[str, str, str]] = [
    ('rabbithole', r'add a subscription \(url', 'label of the "+" item and grouping unverifiable (closed source)'),
    ('rabbithole', r'first connect', 'screen unverified'),
    ('rabbithole', r'sort|fastest', 'third sort option unverified'),
    ('rabbithole', r'switch proxy mode global', 'no way to open Zashboard shown'),
    ('rabbithole', r'keyboard', 'RU shortcut string differs (typo), unverified'),
    ('rabbithole', r'delete or rename', 'entry button unverified'),
]

STOP = {'path', 'the', 'and', 'from', 'for', 'with', 'via', 'one', 'all', 'a', 'to', 'of', 'in', 'on', 'or'}
EXEMPT_RE = re.compile(
    r'(path|paths|sequence)[^.]{0,40}\b(is|are)\s+(correct|accurate)|match the docs|is accurate', re.IGNORECASE
)


def categories_of(text: str) -> set[str]:
    return {cid for cid in CAT_IDS if CAT_RE[cid].search(text)}


def tokens(text: str) -> set[str]:
    return {w for w in re.findall(r'[a-z]{3,}', text.lower()) if w not in STOP}


def issue_lead(issue: str) -> str:
    lead = issue.split(':', 1)[0] if ':' in issue[:90] else issue[:80]
    return lead


def path_contradicted(app_key: str, task: str, issues: list[str]) -> bool:
    for key, pattern, _reason in EXTRA_DROPS:
        if key == app_key and re.search(pattern, task, re.IGNORECASE):
            return True
    t_tokens = tokens(task)
    t_cats = categories_of(task)
    for issue in issues:
        if EXEMPT_RE.search(issue):
            continue
        lead = issue_lead(issue)
        l_tokens = tokens(lead)
        if l_tokens and l_tokens <= t_tokens and categories_of(lead) & t_cats:
            return True
        if len(t_tokens & l_tokens) >= 2 and (categories_of(lead) & t_cats or task.lower() in lead.lower()):
            return True
    return False


def flow_name(flow: str) -> str:
    return re.sub(r'^\d+\s+', '', flow).strip()


def platform_tag(note: str) -> str:
    m = re.search(
        r'\b(PC only|desktop only|mobile only|Android only|iOS only|macOS only|Windows only|Windows/Linux only'
        r'|only (?:on )?(?:PC|desktop|mobile|Android|iOS|macOS|Windows))\b',
        note or '',
        re.IGNORECASE,
    )
    return f' [{m.group(1)}]' if m else ''


def refuted_texts(data: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    for lab in data['labels']:
        if lab.get('verify') == 'refuted':
            out.update(t for t in (lab.get('en'), lab.get('ru')) if t)
    return out


def label_pair(lab: dict[str, Any]) -> tuple[str, str]:
    en = lab.get('en_corrected') or lab['en']
    ru = lab.get('ru_corrected') or lab['ru']
    return en, ru


NOISE_SCREEN_RE = re.compile(
    r'toast|snack|error|alert|message|notification|status text|placeholder|hint', re.IGNORECASE
)
NOISE_TEXT_RE = re.compile(
    r'%|\.\.\.|…|\$|\bis (?:ON|OFF|empty|not)\b|\bnot (?:selected|installed|connected)\b|\b(?:error|failed|success|empty|waiting|problem)\b'
    r'|успешно|ошибк|пуст|ожидани|проблем|\.$',
    re.IGNORECASE,
)
SHOUT_RE = re.compile(r'^[A-Z0-9 ]{6,}$|^[А-ЯЁ0-9 ]{6,}$')
# Ambiguous one-word buttons and strings outside the core tasks (toasts, dialogs, extras).
SKIP_LABELS = {
    'Dashboard',
    'Edit',
    'Copy',
    'Later',
    'Timeout',
    'Enabled',
    'Disabled',
    'Remark',
    'Login',
    'Updated',
    'Save',
    'Stop & save',
    'From subscription',
    'Allow & Continue',
    'Allow & Update',
    'Pin popup',
    'Available traffic',
    'Network speed',
    'Exclude APNs',
    'Set up VPN',
    'Manage devices',
    'Helper service',
    'Collapse all / Expand all',
    'Shortcut',
    'Open main window',
    'Add VPN Configurations',
    'Add configs',
    'Create config',
    'Selector',
    'Proxy',
    'Main Switch',
    'Switch tolerance (ms)',
    'Ping service',
    'Ping delay',
    'Ping mode',
    'Dynamic switch',
    'Alphabetical',
    'Local proxy access',
    'Service Mode',
    'Install Service',
    'Test Group Latency',
    'Group / Proxy',
}
# Labels the skeptic pass flagged as unverified in the app.
UNVERIFIED_LABELS = {('rabbithole', 'Language')}
LIVE_SKIP = {'Backup and Sync', 'Help', 'Reset', 'TUN → DNS Hijack', 'TUN → Strict Route', 'TUN → Default Route'}
PATH_CATS = {'add', 'update', 'mode', 'server', 'tun', 'language', 'connect'}
LABEL_CATS = [c for c in CAT_IDS if c != 'check']
LABEL_MAX = 30


def fmt_label(lab: dict[str, Any]) -> str | None:
    en, ru = label_pair(lab)
    if len(en) > LABEL_MAX or len(ru) > LABEL_MAX or en in SKIP_LABELS:
        return None
    if NOISE_TEXT_RE.search(en) or NOISE_TEXT_RE.search(ru) or SHOUT_RE.match(en):
        return None
    if NOISE_SCREEN_RE.search(lab.get('screen', '')):
        return None
    mark = '*' if lab.get('state') == 'connected' else ''
    body = en if en == ru else f'{en}={ru}'
    return f'{body}{mark}{platform_tag(lab.get("platform_note", ""))}'


def source_section(key: str, data: dict[str, Any], live_texts: set[str]) -> list[str]:
    name = DISPLAY[key]
    tag = data['release_tag']
    kind = 'по исходникам' if data['source_available'] else 'по документации/сборке'
    confirmed = [lab for lab in data['labels'] if lab.get('verify') == 'confirmed']
    lines = [f'### {name} ({kind}, не вживую; {tag})']
    if not confirmed:
        lines.append(
            'Подтверждённых названий кнопок нет (ни подключения, ни режима, ни обновления подписки). '
            'Давать общие шаги без названий кнопок и пометить для владельца.'
        )
        return lines
    refuted = refuted_texts(data) - live_texts
    issues = data['menu_path_issues']
    by_cat: dict[str, list[str]] = {cid: [] for cid in CAT_IDS}
    for lab in confirmed:
        flow = flow_name(lab['flow'])
        cats = categories_of(flow) - ({'check'} if 'log' in flow else set())
        text = fmt_label(lab)
        if not cats or not text or (key, lab['en']) in UNVERIFIED_LABELS or label_pair(lab)[0] in live_texts:
            continue
        for cid in sorted(cats, key=CAT_IDS.index)[:1]:
            if cid == 'check':
                continue
            if text not in by_cat[cid]:
                by_cat[cid].append(text)
    paths: list[str] = []
    for p in data['menu_paths']:
        task = p['task']
        if not categories_of(task) & PATH_CATS or re.search(
            r'check that|^disconnect$|(?:active|live) connections', task, re.IGNORECASE
        ):
            continue
        if key in LIVE_APP_KEYS:
            continue
        if path_contradicted(key, task, issues):
            continue
        if any(t in p['path_en'] or t in p['path_ru'] for t in refuted):
            continue
        paths.append(f'  - {task}: {p["path_en"]} | {p["path_ru"]}')
    for cid in CAT_IDS:
        if by_cat[cid]:
            lines.append(f'- {CAT_CAPTION[cid]}: ' + '; '.join(by_cat[cid]))
    if paths:
        lines.append('- пути (EN | RU):')
        lines += paths
    return lines


def live_sections(clients: dict[str, Any]) -> tuple[list[str], set[str]]:
    lines: list[str] = []
    texts: set[str] = set()
    for entry in clients['apps']:
        if entry.get('status') == 'unverified' or 'states' not in entry:
            continue
        app, plat, ver = entry['app'], entry['platform'], entry['version']
        lines.append(f'### {app} {plat} {ver}: проверено вживую {entry["verified_at"]}')
        if entry.get('note'):
            lines.append(f'Примечание: {entry["note"]}')
        states = entry['states']
        for state_name, st in states.items():
            if st.get('status') != 'verified':
                continue
            label = 'отключено' if state_name == 'disconnected' else 'подключено'
            for sname, scr in (st.get('screens') or {}).items():
                if isinstance(scr, dict):
                    items = scr.get('items') or scr.get('items_ru')
                    if sname in SHOWN_SCREENS and items:
                        lines.append(f'- [{label}] {sname}: ' + '; '.join(items))
                        texts.update(items)
                elif isinstance(scr, list):
                    lines.append(f'- [{label}] {sname}: ' + '; '.join(scr))
                elif isinstance(scr, str):
                    lines.append(f'- [{label}] {sname}: {scr}')
            for tname, task in (st.get('tasks') or {}).items():
                lines.append(f'- [{label}] {tname}: {task}')
        for tname, task in (entry.get('tasks') or {}).items():
            lines.append(f'- {tname}: {task}')
        ru = entry.get('russian_ui')
        if ru and ru.get('status') == 'verified':
            lines.append(f'- Русский интерфейс: {ru["how_to_switch"]}. Не переведено: {ru["partial_translation"]}.')
            pairs = [
                f'{k}={v}' for k, v in ru['labels_en_ru'].items() if k not in LIVE_SKIP and k != 'Diversion Template'
            ]
            lines.append('- EN=RU: ' + '; '.join(pairs))
            texts.update(ru['labels_en_ru'].keys())
            texts.update(ru['labels_en_ru'].values())
    return lines, texts


SHOWN_SCREENS = {'main', 'proxies', 'network_check', 'my_profiles', 'add_profile_sheet', 'profile_menu'}

HEADER = """\
<!-- GENERATED FILE: do not edit. Source: docs/support-kb/clients.yaml and docs/support-kb/source/*.json.
     Regenerate: uv run python scripts/build_support_kb.py -->
# Знания о клиентских приложениях (проверенные)
Правило: называть экран, кнопку или настройку приложения можно только если они перечислены здесь для этого приложения. Язык названий — язык интерфейса клиента: RU для русскоязычных, иначе EN. EN=RU: английское название слева, русское справа. «*» = пункт виден только при активном подключении. «вживую» = прочитано в запущенном приложении; «по исходникам / по документации» = сверено с кодом или докой выпуска, не открывалось на устройстве, версия клиента может отличаться. Приложения и платформы, которых здесь нет, не проверены: давать общие шаги без названий кнопок и пометить для владельца.
"""


def build() -> str:
    clients = yaml.safe_load((KB_DIR / 'clients.yaml').read_text(encoding='utf-8'))
    out = [HEADER, '## Проверено вживую']
    live, live_texts = live_sections(clients)
    out += live
    out.append('')
    out.append('## Сверено с исходниками / документацией (платформы не уточнены; * — только при подключении)')
    for key in APP_ORDER:
        data = json.loads((KB_DIR / 'source' / f'{key}.json').read_text(encoding='utf-8'))
        out += source_section(key, data, live_texts)
    return '\n'.join(out).rstrip() + '\n'


def main() -> int:
    content = build()
    if '--check' in sys.argv:
        return 0 if OUT.exists() and OUT.read_text(encoding='utf-8') == content else 1
    OUT.write_text(content, encoding='utf-8')
    print(f'wrote {OUT} ({len(content)} chars)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
