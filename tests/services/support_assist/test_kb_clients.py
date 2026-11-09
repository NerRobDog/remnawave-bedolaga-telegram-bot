"""kb_clients.md is generated from docs/support-kb; it must stay in sync, verified-only, small, and reach the model."""

import importlib.util
import json
from pathlib import Path

import yaml

from app.services.support_assist import card


ROOT = Path(__file__).resolve().parents[3]
KB_DIR = ROOT / 'docs' / 'support-kb'
GENERATED = ROOT / 'app' / 'services' / 'support_assist' / 'kb_clients.md'
SIZE_CAP = 30_000  # ~9-10k tokens, sits in the cached system block


def _builder():
    spec = importlib.util.spec_from_file_location('build_support_kb', ROOT / 'scripts' / 'build_support_kb.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_generated_file_is_up_to_date():
    expected = _builder().build()
    assert GENERATED.read_text(encoding='utf-8') == expected, (
        'kb_clients.md is stale: run `uv run python scripts/build_support_kb.py` and commit the result'
    )


def test_no_refuted_label_text_in_output():
    content = GENERATED.read_text(encoding='utf-8')
    live = yaml.safe_load((KB_DIR / 'clients.yaml').read_text(encoding='utf-8'))
    live_blob = json.dumps(live, ensure_ascii=False)
    checked = 0
    for path in sorted((KB_DIR / 'source').glob('*.json')):
        data = json.loads(path.read_text(encoding='utf-8'))
        confirmed = {
            t
            for lab in data['labels']
            if lab['verify'] == 'confirmed'
            for t in (lab.get('en_corrected') or lab['en'], lab.get('ru_corrected') or lab['ru'])
        }
        for lab in data['labels']:
            if lab['verify'] != 'refuted':
                continue
            for text in (lab['en'], lab['ru']):
                # A string can be refuted for one flow yet legitimately confirmed elsewhere or live-verified.
                if len(text) < 12 or text in confirmed or text in live_blob:
                    continue
                checked += 1
                assert text not in content, f'refuted label in kb_clients.md: {text!r} ({path.name})'
    assert checked > 0


def test_no_unverified_markers_or_happ_buttons():
    content = GENERATED.read_text(encoding='utf-8')
    assert 'not_checked' not in content
    happ = content.split('### Happ', 1)[1]
    assert 'Подтверждённых названий кнопок нет' in happ


def test_size_cap():
    assert len(GENERATED.read_text(encoding='utf-8')) <= SIZE_CAP


def test_system_blocks_contain_client_knowledge():
    blocks = card._system_blocks()
    text = '\n'.join(b['text'] for b in blocks)
    clients = GENERATED.read_text(encoding='utf-8')
    assert clients in text
    assert 'Добавление подписки' in text
    assert 'Мои профили' not in text
    assert blocks[-1].get('cache_control') == {'type': 'ephemeral'}
    assert clients in blocks[-1]['text']


def test_global_mode_is_server_pick_via_vpn():
    # Since 2026-11-08 the subscription templates carry their own GLOBAL group:
    # only the servers, first one by default. Global = VPN with a manual server pick.
    text = '\n'.join(b['text'] for b in card._system_blocks())
    assert 'мимо VPN' not in text
    assert 'DIRECT by default' not in text
    assert 'Never advise Global' not in text
    assert 'выбрать конкретный сервер' in text
    assert 'обновить подписку' in text
    assert 'GLOBAL' in GENERATED.read_text(encoding='utf-8')


def test_live_android_entries_present():
    content = GENERATED.read_text(encoding='utf-8')
    for header in ('### Clash Mi android 1.0.31', '### FlClashX android 0.4.2', '### Happ android 4.9.0'):
        assert header in content
    assert 'Слишком дофига устройств' in content


def test_prompt_has_client_app_rule():
    prompt = card._system_blocks()[0]['text']
    assert 'клиентских приложениях' in prompt
    assert 'Happ' in prompt
