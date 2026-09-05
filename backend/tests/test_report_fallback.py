from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.templates.report_fallback import (
    format_period_label,
    render_report_fallback,
)


def test_format_period_label_day() -> None:
    assert format_period_label("2026-09-05_day", "day") == "05.09.2026 (день)"


def test_format_period_label_week() -> None:
    label = format_period_label("2026-W36_week", "week")
    start = date.fromisocalendar(2026, 36, 1)
    end = start.toordinal() + 6
    end_date = date.fromordinal(end)
    expected = (
        f"{start.strftime('%d.%m')}–{end_date.strftime('%d.%m.%Y')} (неделя)"
    )
    assert label == expected


def test_format_period_label_request() -> None:
    assert format_period_label("request_42", "day") == "Заявка #42"


def test_render_report_fallback_empty_day_layout() -> None:
    text = render_report_fallback(
        {
            "period": "day",
            "period_key": "2026-09-05_day",
            "requests_total": 0,
            "deals_closed": 0,
            "messages_out_total": 0,
            "messages_in_total": 0,
            "avg_price_initial": None,
            "avg_final_price": None,
            "savings_vs_initial": None,
            "open_requests": 0,
            "top_suppliers": [],
        }
    )
    assert "<b>Отчёт закупок</b>" in text
    assert "05.09.2026 (день)" in text
    assert "<b>Активность</b>" in text
    assert "<b>Цены</b>" not in text
    assert "За период активности не зафиксировано." in text
    assert "нет данных" in text
    assert "н/д" not in text


def test_render_report_fallback_with_prices_and_top_suppliers() -> None:
    text = render_report_fallback(
        {
            "period": "day",
            "period_key": "2026-09-05_day",
            "requests_total": 2,
            "deals_closed": 1,
            "messages_out_total": 3,
            "messages_in_total": 2,
            "avg_price_initial": Decimal("100000"),
            "avg_final_price": Decimal("95000"),
            "savings_vs_initial": Decimal("5000"),
            "open_requests": 1,
            "top_suppliers": [{"name": "Supp", "deals_count": 1}],
        }
    )
    assert "<b>Цены</b>" in text
    assert "100 000 ₽" in text
    assert "95 000 ₽" in text
    assert "5 000 ₽" in text
    assert "1. Supp — 1 сделка" in text
