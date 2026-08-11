"""Fallback HTML report formatter without LLM (TECH DOC §8.4)."""

from __future__ import annotations

from typing import Any

from jinja2 import Environment

_TEMPLATE = Environment(autoescape=True).from_string(
    """
<b>Отчет закупок</b>
<b>Период:</b> {{ period_key }}

<b>Заявки:</b> {{ requests_total }}
<b>Сделки закрыты:</b> {{ deals_closed }}
<b>Сообщения поставщикам:</b> {{ messages_out_total }}
<b>Ответы поставщиков:</b> {{ messages_in_total }}

<b>Средняя стартовая цена:</b> {{ avg_price_initial }}
<b>Средняя итоговая цена:</b> {{ avg_final_price }}
<b>Экономия к старту:</b> {{ savings_vs_initial }}
<b>Открытые заявки:</b> {{ open_requests }}

<b>Топ поставщиков:</b>
{% if top_suppliers %}
{% for item in top_suppliers -%}
- {{ item.name }}: {{ item.deals_count }}
{% endfor %}
{% else %}
н/д
{% endif %}
""".strip()
)


def _nd(value: Any) -> str:
    if value is None:
        return "н/д"
    return str(value)


def render_report_fallback(metrics: dict[str, Any]) -> str:
    payload = {
        "period_key": _nd(metrics.get("period_key")),
        "requests_total": _nd(metrics.get("requests_total")),
        "deals_closed": _nd(metrics.get("deals_closed")),
        "messages_out_total": _nd(metrics.get("messages_out_total")),
        "messages_in_total": _nd(metrics.get("messages_in_total")),
        "avg_price_initial": _nd(metrics.get("avg_price_initial")),
        "avg_final_price": _nd(metrics.get("avg_final_price")),
        "savings_vs_initial": _nd(metrics.get("savings_vs_initial")),
        "open_requests": _nd(metrics.get("open_requests")),
        "top_suppliers": metrics.get("top_suppliers") or [],
    }
    return _TEMPLATE.render(**payload).strip()
