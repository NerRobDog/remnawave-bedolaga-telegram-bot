import pytest

from app.cabinet.services.email_templates import EmailNotificationTemplates
from app.services.notification_types import NotificationType


@pytest.mark.parametrize('lang', ['ru', 'en', 'zh', 'ua'])
def test_traffic_warning_template_renders(lang):
    tpl = EmailNotificationTemplates().get_template(
        NotificationType.TRAFFIC_WARNING,
        lang,
        {'used_gb': '90.0', 'limit_gb': 100, 'percent': '90'},
    )
    assert tpl and tpl['subject']
    assert '90' in tpl['body_html'] and '100' in tpl['body_html']


@pytest.mark.parametrize('lang', ['ru', 'en', 'zh', 'ua'])
def test_balance_low_template_renders(lang):
    tpl = EmailNotificationTemplates().get_template(
        NotificationType.BALANCE_LOW,
        lang,
        {'formatted_balance': '5 ₽', 'formatted_threshold': '100 ₽'},
    )
    assert tpl and tpl['subject']
    assert '5 ₽' in tpl['body_html'] and '100 ₽' in tpl['body_html']
