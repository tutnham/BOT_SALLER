"""Fallback HTML report formatter without LLM (TECH DOC §8.4)."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from jinja2 import Environment

_TEMPLATE = Environment(autoescape=True).from_string(
    """
<b>Отчёт закупок</b>
<i>Период: {{ period_label }}</i>

<b>Активность</b>
· Заявки: {{ requests_total }}
· Сделки закрыты: {{ deals_closed }}
· Сообщения поставщикам: {{ messages_out_total }}
· Ответы поставщиков: {{ messages_in_total }}
· Открытые заявки: {{ open_requests }}
{% if is_empty %}
<i>За период активности не зафиксировано.</i>
{% endif %}
{% if show_prices %}

<b>Цены</b>
· Средняя стартовая: {{ avg_price_initial }}
· Средняя итоговая: {{ avg_final_price }}
· Экономия к старту: {{ savings_vs_initial }}
{% endif %}

<b>Топ поставщиков</b>
{% if top_suppliers %}
{% for item in top_suppliers -%}
{{ loop.index }}. {{ item.name }} — {{ item.deals_count }} {{ item.deals_word }}
{% endfor %}
{% else %}
нет данных
{% endif %}
""".strip()
)


def format_period_label(period_key: str, period: str) -> str:
    if period_key.startswith("request_"):
        return f"Заявка #{period_key.removeprefix('request_')}"

    if period_key.endswith("_day"):
        day = date.fromisoformat(period_key.removesuffix("_day"))
        return f"{day.strftime('%d.%m.%Y')} (день)"

    if period_key.endswith("_week"):
        week_part = period_key.removesuffix("_week")
        year_str, week_str = week_part.split("-W", maxsplit=1)
        start = date.fromisocalendar(int(year_str), int(week_str), 1)
        end = start + timedelta(days=6)
        return f"{start.strftime('%d.%m')}–{end.strftime('%d.%m.%Y')} (неделя)"

    if period == "day":
        return f"{period_key} (день)"
    if period == "week":
        return f"{period_key} (неделя)"
    return period_key


def _format_money(value: Any) -> str:
    if value is None:
        return "—"
    amount = value if isinstance(value, Decimal) else Decimal(str(value))
    quantized = amount.quantize(Decimal("0.01"))
    if quantized == quantized.to_integral_value():
        formatted = f"{int(quantized):,}".replace(",", " ")
    else:
        formatted = f"{quantized:,.2f}".replace(",", " ")
    return f"{formatted} ₽"


def _deals_word(count: int) -> str:
    n_abs = abs(count) % 100
    if 11 <= n_abs <= 19:
        return "сделок"
    remainder = n_abs % 10
    if remainder == 1:
        return "сделка"
    if 2 <= remainder <= 4:
        return "сделки"
    return "сделок"


def render_report_fallback(metrics: dict[str, Any]) -> str:
    requests_total = int(metrics.get("requests_total") or 0)
    deals_closed = int(metrics.get("deals_closed") or 0)
    messages_out_total = int(metrics.get("messages_out_total") or 0)
    messages_in_total = int(metrics.get("messages_in_total") or 0)
    open_requests = int(metrics.get("open_requests") or 0)
    period = str(metrics.get("period") or "")
    period_key = str(metrics.get("period_key") or "")

    top_suppliers = [
        {
            "name": item["name"],
            "deals_count": int(item["deals_count"]),
            "deals_word": _deals_word(int(item["deals_count"])),
        }
        for item in (metrics.get("top_suppliers") or [])
    ]

    is_empty = (
        requests_total == 0
        and deals_closed == 0
        and messages_out_total == 0
        and messages_in_total == 0
    )
    show_prices = (
        deals_closed > 0
        or metrics.get("avg_price_initial") is not None
        or metrics.get("avg_final_price") is not None
        or metrics.get("savings_vs_initial") is not None
    )

    payload = {
        "period_label": format_period_label(period_key, period),
        "requests_total": requests_total,
        "deals_closed": deals_closed,
        "messages_out_total": messages_out_total,
        "messages_in_total": messages_in_total,
        "open_requests": open_requests,
        "is_empty": is_empty,
        "show_prices": show_prices,
        "avg_price_initial": _format_money(metrics.get("avg_price_initial")),
        "avg_final_price": _format_money(metrics.get("avg_final_price")),
        "savings_vs_initial": _format_money(metrics.get("savings_vs_initial")),
        "top_suppliers": top_suppliers,
    }
    return _TEMPLATE.render(**payload).strip()
