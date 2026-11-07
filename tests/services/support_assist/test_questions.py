from app.services.support_assist.flags import Flag
from app.services.support_assist.questions import (
    QUESTION_IDS,
    is_bare,
    pick_questions,
    render_questions,
)

from .fixtures import healthy


DETAILED = (
    'Вчера вечером перестал открываться Telegram через мобильную сеть оператора в Белграде, '
    'хотя днём всё работало нормально, пробовал переключить сервер и перезапустить приложение дважды'
)


def test_bare_short_complaint():
    assert is_bare('не работает', False) is True


def test_detailed_message_not_bare():
    assert len(DETAILED.split()) >= 15
    assert is_bare(DETAILED, False) is False


def test_empty_text_with_media_is_bare():
    assert is_bare('', True) is True
    assert is_bare(None, True) is True


def test_short_with_few_details_is_bare():
    assert is_bare('не работает vpn на телефоне', False) is True


def test_our_fault_flag_gives_no_questions():
    s = healthy()
    flags = [Flag('sub_expired', 'high', 'x')]
    assert pick_questions('не работает', False, s, flags) == []


def test_photo_drops_screenshot():
    s = healthy(devices=[])
    assert 'screenshot' not in pick_questions('', True, s, [])
    assert 'screenshot' in pick_questions('не работает', False, s, [])


def test_known_devices_drop_device_question():
    s = healthy()
    assert 'device' not in pick_questions('не работает', False, s, [])
    s2 = healthy(devices=[])
    assert 'device' in pick_questions('не работает', False, s2, [])


def test_text_naming_other_device_keeps_device_question():
    s = healthy()  # iOS + macOS
    assert 'device' in pick_questions('не работает на телевизоре', False, s, [])


def test_app_named_drops_app_question():
    s = healthy(devices=[])
    ids = pick_questions('в Happ не грузит', False, s, [])
    assert 'app' not in ids
    assert 'app' in pick_questions('не грузит', False, s, [])


def test_order_follows_question_ids():
    s = healthy(devices=[])
    ids = pick_questions('не работает', False, s, [])
    assert ids == [q for q in QUESTION_IDS if q in ids]
    assert ids[0] == 'device'


def test_render_english():
    text = render_questions(['device'], 'en')
    assert 'device' in text.lower()
    assert 'устрой' not in text.lower()


def test_render_russian_has_intro_and_numbering():
    text = render_questions(['device', 'app'], 'ru')
    assert '1.' in text and '2.' in text
    assert 'устрой' in text.lower()


def test_render_all_ids_resolve_in_both_languages():
    for lang in ('ru', 'en'):
        out = render_questions(list(QUESTION_IDS), lang)
        assert 'SUPPORT_ASSIST_Q' not in out
