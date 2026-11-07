"""Ручной прогон карточек поддержки на фикстурах S01-S15 и своих кейсах.

Тратит реальные деньги (вызовы Claude), поэтому без ``--yes-spend`` не запускается.

    uv run python scripts/support_assist_eval.py --yes-spend --out /tmp/eval
    uv run python scripts/support_assist_eval.py --yes-spend --out /tmp/eval --cases my_cases/

Формат своего кейса: файл ``*.md``; заголовок ``# ...`` необязателен, остальное — сообщения клиента,
несколько сообщений разделяются строкой ``---``. Снимок для такого кейса — здоровый клиент без флагов.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    from app.services.support_assist.card import CardResult

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # запуск как файла: корень репозитория в sys.path


DEFAULT_MESSAGE = 'Не работает впн'
CSV_FIELDS = ['case', 'model', 'input_tokens', 'output_tokens', 'cache_read_tokens', 'cost_usd', 'error']

Generate = Callable[[Any, dict, list[str]], Awaitable['CardResult']]


@dataclass
class Case:
    name: str
    payload: dict
    messages: list[str]


def builtin_cases() -> list[Case]:
    # Импорты внутри функций: --help и отказ без --yes-spend работают без .env (config требует BOT_TOKEN).
    from app.services.support_assist.flags import compute_flags  # noqa: PLC0415
    from app.services.support_assist.payload import build_payload  # noqa: PLC0415
    from tests.services.support_assist.fixtures import SCENARIOS  # noqa: PLC0415

    cases = []
    for code, (builder, _must, _must_not) in SCENARIOS.items():
        snapshot = builder()
        cases.append(Case(code, build_payload(snapshot, compute_flags(snapshot)), [DEFAULT_MESSAGE]))
    return cases


def parse_case(path: Path) -> tuple[str, list[str]]:
    lines = path.read_text(encoding='utf-8').splitlines()
    if lines and lines[0].startswith('#'):
        lines = lines[1:]
    messages = [part.strip() for part in '\n'.join(lines).split('\n---\n')]
    return path.stem, [m for m in messages if m]


def load_cases_dir(directory: Path) -> list[Case]:
    from app.services.support_assist.payload import build_payload  # noqa: PLC0415
    from tests.services.support_assist.fixtures import healthy  # noqa: PLC0415

    snapshot = healthy()
    payload = build_payload(snapshot, [])
    cases = []
    for path in sorted(directory.glob('*.md')):
        name, messages = parse_case(path)
        cases.append(Case(name, payload, messages))
    return cases


async def run_cases(client: Any, cases: list[Case], out: Path, generate: Generate | None = None) -> Decimal:
    """Прогнать кейсы по очереди, записать карточки и usage.csv; вернуть суммарную цену."""
    if generate is None:
        from app.services.support_assist.card import generate_card  # noqa: PLC0415

        generate = generate_card
    out.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240
    total = Decimal(0)
    with (out / 'usage.csv').open('w', encoding='utf-8', newline='') as fh:
        writer = csv.writer(fh)
        writer.writerow(CSV_FIELDS)
        for case in cases:
            result = await generate(client, case.payload, case.messages)
            record = {
                'case': case.name,
                'messages': case.messages,
                'card': result.card.model_dump(mode='json') if result.card else None,
                'model': result.model,
                'input_tokens': result.input_tokens,
                'output_tokens': result.output_tokens,
                'cache_read_tokens': result.cache_read_tokens,
                'cost_usd': str(result.cost_usd) if result.cost_usd is not None else None,
                'error': result.error,
            }
            (out / f'{case.name}.json').write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
            writer.writerow([record[field] if field != 'case' else case.name for field in CSV_FIELDS])
            if result.cost_usd is not None:
                total += result.cost_usd
    return total


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Ручной прогон карточек поддержки (тратит деньги).')
    parser.add_argument('--out', type=Path, required=True, help='каталог для карточек и usage.csv')
    parser.add_argument('--cases', type=Path, help='каталог с *.md кейсами сверх S01-S15')
    parser.add_argument('--yes-spend', action='store_true', help='подтвердить трату реальных денег')
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not args.yes_spend:
        print('Отказ: прогон тратит реальные деньги, добавь --yes-spend.', file=sys.stderr)
        return 2
    import anthropic  # noqa: PLC0415

    from app.config import settings  # noqa: PLC0415

    if not settings.ANTHROPIC_API_KEY:
        print('Отказ: ANTHROPIC_API_KEY не задан.', file=sys.stderr)
        return 2

    cases = builtin_cases()
    if args.cases:
        cases += load_cases_dir(args.cases)

    async def go() -> Decimal:
        async with anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY) as client:
            return await run_cases(client, cases, args.out)

    total = asyncio.run(go())
    print(f'Готово: {len(cases)} кейсов, итого ${total:.4f}, результат в {args.out}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
