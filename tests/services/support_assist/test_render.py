from app.services.support_assist.card import Card, CardFact
from app.services.support_assist.flags import Flag
from app.services.support_assist.render import build_card_keyboard, render_card


def _card(**over):
    data = dict(
        facts=[CardFact(icon='⚠️', text='Устройств 3 из 3')],
        cause='Лимит устройств',
        owner_action='Сбросить устройства',
        ask_client=['Какое приложение?'],
        draft='Удалите лишние устройства.',
    )
    data.update(over)
    return Card(**data)


FLAGS = [
    Flag('sub_expired', 'high', 'Подписка истекла'),
    Flag('x', 'mid', 'Много устройств'),
    Flag('ok', 'info', 'Всё в порядке'),
]


def test_full_card_has_all_sections():
    text = render_card(_card(), FLAGS, 42)
    for marker in ('🔎', '💡', '🛠', '❓', '✏️', '42'):
        assert marker in text


def test_fallback_card_without_model():
    text = render_card(None, FLAGS, 7)
    assert 'разбор без модели' in text
    assert '✏️' not in text
    assert '🔴' in text and '⚠️' in text and '✅' in text


def test_long_draft_fits_telegram_limit():
    text = render_card(_card(draft='я' * 10_000), FLAGS, 1)
    assert len(text) <= 4096
    assert 'Лимит устройств' in text


def test_everything_huge_fits_limit():
    big = 'ы<' * 5000
    card = _card(
        facts=[CardFact(icon='✅', text=big) for _ in range(7)],
        cause=big,
        owner_action=big,
        ask_client=[big, big],
        draft=big,
    )
    assert len(render_card(card, FLAGS, 1)) <= 4096


def test_html_is_escaped():
    text = render_card(_card(draft='<script>alert(1)</script>', cause='a & b'), FLAGS, 1)
    assert '<script>' not in text
    assert '&lt;script&gt;' in text and 'a &amp; b' in text


def test_keyboard_without_send():
    kb = build_card_keyboard(5, show_send=False)
    data = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert data == ['sa:skip:5']


def test_keyboard_with_send():
    kb = build_card_keyboard(5, show_send=True)
    data = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert data == ['sa:send:5', 'sa:edit:5', 'sa:skip:5']
