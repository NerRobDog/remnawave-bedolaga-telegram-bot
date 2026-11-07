from app.services.support_assist.card import Card, CardFact
from app.services.support_assist.flags import Flag
from app.services.support_assist.render import (
    TRUNCATED_DRAFT_NOTICE,
    build_card_keyboard,
    render_card,
    render_card_checked,
)


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


def test_fallback_card_marks_unavailable_data_with_question_mark():
    flags = [Flag('data_unavailable', 'unknown', 'Нет данных: устройства'), Flag('ok', 'info', 'Всё в порядке')]
    text = render_card(None, flags, 7)
    assert '❓ Нет данных: устройства' in text


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


def test_custom_limit_leaves_room_for_the_decision_line():
    line = '\n\n✅ отправлено'
    text = render_card(_card(draft='я' * 10_000), FLAGS, 1, limit=4096 - len(line))
    assert len(text) <= 4096 - len(line)
    assert len(text + line) <= 4096
    assert 'Лимит устройств' in text


def test_default_limit_is_unchanged():
    assert render_card(_card(), FLAGS, 1) == render_card(_card(), FLAGS, 1, limit=4096)


# --- черновик, который не поместился: «Отправить» только для того, что владелец увидел целиком -------------------


def test_short_draft_is_not_marked_truncated():
    rendered = render_card_checked(_card(), FLAGS, 1)
    assert rendered.draft_truncated is False
    assert rendered.text == render_card(_card(), FLAGS, 1)


def test_long_draft_is_marked_truncated():
    rendered = render_card_checked(_card(draft='я' * 10_000), FLAGS, 1)
    assert rendered.draft_truncated is True
    assert len(rendered.text) <= 4096


def test_draft_dropped_entirely_counts_as_truncated():
    big = 'ы' * 5000
    rendered = render_card_checked(_card(cause=big, owner_action=big, draft='Короткий черновик'), FLAGS, 1)
    assert 'Короткий черновик' not in rendered.text
    assert rendered.draft_truncated is True


def test_draft_that_fits_exactly_is_not_truncated():
    base = render_card(_card(draft='x'), FLAGS, 1)
    room = 4096 - len(base) + 1
    draft = 'x' * room
    rendered = render_card_checked(_card(draft=draft), FLAGS, 1)
    assert len(rendered.text) == 4096
    assert rendered.draft_truncated is False
    assert draft in rendered.text


def test_other_sections_cut_with_the_draft_intact_do_not_mark_it():
    # порядок урезания начинается с черновика: пока он цел, остальное не трогают
    rendered = render_card_checked(_card(draft='Короткий черновик'), FLAGS, 1)
    assert rendered.draft_truncated is False


def test_card_without_a_draft_or_model_is_never_truncated():
    assert render_card_checked(_card(draft=''), FLAGS, 1).draft_truncated is False
    assert render_card_checked(None, FLAGS, 1).draft_truncated is False


def test_html_heavy_draft_is_judged_by_the_escaped_length():
    # '&' становится '&amp;': по исходной длине влезает, по разметке нет
    rendered = render_card_checked(_card(draft='&' * 3900), FLAGS, 1)
    assert rendered.draft_truncated is True
    assert len(rendered.text) <= 4096


def test_notice_is_appended_only_when_the_draft_was_cut():
    cut = render_card_checked(_card(draft='я' * 10_000), FLAGS, 1, truncation_notice=TRUNCATED_DRAFT_NOTICE)
    assert cut.draft_truncated is True
    assert cut.text.endswith(TRUNCATED_DRAFT_NOTICE)
    assert 'Править' in TRUNCATED_DRAFT_NOTICE
    assert len(cut.text) <= 4096

    whole = render_card_checked(_card(), FLAGS, 1, truncation_notice=TRUNCATED_DRAFT_NOTICE)
    assert whole.draft_truncated is False
    assert TRUNCATED_DRAFT_NOTICE not in whole.text


def test_notice_fits_next_to_a_dropped_draft_and_a_huge_card():
    big = 'ы<' * 5000
    card = _card(
        facts=[CardFact(icon='✅', text=big) for _ in range(7)],
        cause=big,
        owner_action=big,
        ask_client=[big, big],
        draft=big,
    )
    rendered = render_card_checked(card, FLAGS, 1, truncation_notice=TRUNCATED_DRAFT_NOTICE)
    assert rendered.draft_truncated is True
    assert len(rendered.text) <= 4096
    assert rendered.text.endswith(TRUNCATED_DRAFT_NOTICE)


def test_keyboard_for_a_truncated_draft_has_edit_but_no_send():
    kb = build_card_keyboard(5, show_send=True, draft_truncated=True)
    data = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert data == ['sa:edit:5', 'sa:skip:5']


def test_keyboard_flag_is_irrelevant_when_send_buttons_are_off():
    kb = build_card_keyboard(5, show_send=False, draft_truncated=True)
    assert [b.callback_data for row in kb.inline_keyboard for b in row] == ['sa:skip:5']
