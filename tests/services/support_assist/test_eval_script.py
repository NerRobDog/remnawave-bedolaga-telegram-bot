"""Ручной прогон оценки: защита от случайной траты денег и разбор файлов с кейсами."""

from __future__ import annotations

import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from app.services.support_assist.card import Card, CardFact, CardResult
from scripts import support_assist_eval as ev


ROOT = Path(__file__).resolve().parents[3]


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603
        [sys.executable, '-m', 'scripts.support_assist_eval', *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_help_exits_zero() -> None:
    assert _run('--help').returncode == 0


def test_refuses_without_yes_spend(tmp_path: Path) -> None:
    proc = _run('--out', str(tmp_path))
    assert proc.returncode != 0
    assert '--yes-spend' in proc.stderr
    assert list(tmp_path.iterdir()) == []


def test_parse_case_file(tmp_path: Path) -> None:
    path = tmp_path / 'R01.md'
    path.write_text('# Не грузит\n\nПервое сообщение\n\n---\n\nВторое сообщение\n', encoding='utf-8')
    name, messages = ev.parse_case(path)
    assert name == 'R01'
    assert messages == ['Первое сообщение', 'Второе сообщение']


def test_builtin_cases_cover_s01_s15() -> None:
    names = [c.name for c in ev.builtin_cases()]
    assert names == [f'S{i:02d}' for i in range(1, 16)]


@pytest.mark.asyncio
async def test_run_writes_cards_and_csv(tmp_path: Path) -> None:
    card = Card(facts=[CardFact(icon='✅', text='ok')], cause='c', owner_action='a', ask_client=['q'], draft='d')

    async def fake_generate(client, payload, messages):
        return CardResult(
            card=card,
            model='m',
            input_tokens=10,
            output_tokens=5,
            cache_read_tokens=2,
            cost_usd=Decimal('0.01'),
        )

    cases = ev.builtin_cases()[:2]
    await ev.run_cases(None, cases, tmp_path, generate=fake_generate)
    assert (tmp_path / 'S01.json').exists()
    rows = (tmp_path / 'usage.csv').read_text(encoding='utf-8').splitlines()
    assert rows[0].startswith('case,model,input_tokens')
    assert len(rows) == 3
