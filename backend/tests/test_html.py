from __future__ import annotations

from app.utils.html import (
    TELEGRAM_HTML_PARSE_MODE,
    sanitize_telegram_html,
    sanitize_telegram_report_html,
)


def test_telegram_html_parse_mode_constant() -> None:
    assert TELEGRAM_HTML_PARSE_MODE == "HTML"


def test_sanitize_telegram_html_escapes_tags() -> None:
    assert sanitize_telegram_html("<b>hack</b>") == "&lt;b&gt;hack&lt;/b&gt;"


def test_sanitize_telegram_report_html_keeps_allowed_tags() -> None:
    source = "<b>Отчёт</b>\n<i>Период: 05.09.2026 (день)</i>"
    assert sanitize_telegram_report_html(source) == source


def test_sanitize_telegram_report_html_strips_script_tags() -> None:
    source = "<b>Отчёт</b><script>alert(1)</script>"
    assert sanitize_telegram_report_html(source) == "<b>Отчёт</b>"


def test_sanitize_telegram_report_html_strips_unsafe_links() -> None:
    source = '<a href="javascript:alert(1)">click</a>'
    assert sanitize_telegram_report_html(source) == ""


def test_sanitize_telegram_report_html_keeps_safe_links() -> None:
    source = '<a href="https://example.com">site</a>'
    assert sanitize_telegram_report_html(source) == source


def test_sanitize_telegram_report_html_nested_allowed_tags_remain() -> None:
    source = "<b>Топ</b>\n1. <b>Supp</b> — 1 сделка"
    assert sanitize_telegram_report_html(source) == source
