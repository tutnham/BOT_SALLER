"""Pre-defined outgoing message templates (TECH DOC §10)."""

from __future__ import annotations

from typing import Any

TEMPLATES: dict[str, str] = {
    "ask": (
        "Запрос #{request_id}\n"
        "{model} {storage} {color} {region} {sim}\n"
        "Количество: {qty}\n"
        "Уточните, пожалуйста: наличие, актуальную цену, минимальную цену продажи.\n"
        "Ответьте на это сообщение (reply)."
    ),
    "bargain": (
        "По заявке #{request_id} — есть возможность сделать цену {target_price} ₽?\n"
        "Ответьте на это сообщение (reply)."
    ),
    "recheck": (
        "Уточните, пожалуйста, актуальна ли цена по заявке #{request_id}?\n"
        "Ответьте на это сообщение (reply)."
    ),
    "ask_empty": "Нужен текст запроса или reply на сообщение клиента",
    "ask_sent": "Запрос #{request_id} отправлен {N} поставщикам",
    "supplier_need_reply": (
        "Пожалуйста, ответьте на сообщение с номером заявки (#N)"
    ),
    "supplier_quote_parsed": (
        "От: {supplier_name} · Заявка #{request_id}\n"
        "Наличие: {available_text}\n"
        "Цена: {price_text}\n"
        "Кол-во: {qty_text}\n"
        "{raw_text}"
    ),
    "supplier_low_confidence": (
        "От: {supplier_name} · Заявка #{request_id}\n"
        "[распознавание неуверенное]\n"
        "{raw_text}"
    ),
    "deal_closed": (
        "Заявка #{request_id} закрыта. Поставщик: {supplier_name}. "
        "Цена: {final_price} ₽"
    ),
    "setprice_ok": (
        "Цена зафиксирована: заявка #{request_id}, поставщик {supplier_name}, "
        "{price} ₽"
    ),
    "status_summary": (
        "Заявка #{request_id}\n"
        "Статус: {status}\n"
        "Текст: {source_text}\n\n"
        "Предложения:\n{quotes_block}\n"
        "{deal_block}"
    ),
    "cancel_ok": "Заявка #{request_id} отменена",
    "request_not_found": "Заявка #{request_id} не найдена",
    "supplier_not_found": "Поставщик #{supplier_id} не найден",
    "request_not_open": "Заявка #{request_id} уже закрыта или отменена",
    "deal_already_exists": "По заявке #{request_id} уже есть сделка",
    "invalid_command": "Неверный формат команды",
    "bargain_sent": (
        "Торг по заявке #{request_id}: запрошена цена {target_price} ₽ "
        "у поставщика {supplier_name}"
    ),
    "bargain_not_allowed": (
        "Заявка #{request_id}: торг недоступен в статусе {status}"
    ),
    "bargain_no_quote": (
        "Заявка #{request_id}: нет предложений для торга"
    ),
    "bargain_supplier_unavailable": (
        "Заявка #{request_id}: поставщик недоступен для ЛС"
    ),
    "bargain_need_price": (
        "Укажите целевую цену для торга, например: "
        "«пройдём ли по цене ниже 82000 по #N»"
    ),
    "recheck_scheduled": (
        "Заявка #{request_id}: повторная проверка через {hours} ч "
        "(примерно {due_at})"
    ),
    "recheck_invalid_hours": (
        "Интервал повторной проверки должен быть от {min_hours} до "
        "{max_hours} часов"
    ),
    "recheck_skipped": (
        "Заявка #{request_id}: повторная проверка пропущена — "
        "нет доступного поставщика"
    ),
    "price_changed": (
        "Цена по заявке #{request_id} изменилась: было {old_price} ₽, "
        "стало {new_price} ₽"
    ),
    "price_received": "Прайс получен, обработаем при ближайшем утреннем прогоне",
    "price_draft": (
        "Черновик прайса #{draft_id}\n\n"
        "{items_block}\n\n"
        "/approve_price {draft_id}\n"
        "/reject_price {draft_id}"
    ),
    "price_list_published": "Прайс:\n{items_block}",
    "price_approved": "Прайс #{draft_id} утверждён и опубликован",
    "price_rejected": "Прайс #{draft_id} отклонён",
    "price_draft_not_found": "Черновик прайса #{draft_id} не найден",
    "price_draft_already_processed": (
        "Черновик прайса #{draft_id} уже обработан"
    ),
    "owner_start_ok": (
        "Доступ к отчётам открыт.\n"
        "/report day — отчёт за день\n"
        "/report week — отчёт за неделю\n"
        "/report {{id}} — отчёт по заявке\n"
        "/stats week — синоним /report week"
    ),
    "supplier_start_ok": (
        "Здравствуйте! Вы подключены как поставщик.\n"
        "Отвечайте reply на сообщение с номером заявки (#N)."
    ),
    "deal_need_args": (
        "Укажите заявку, поставщика и цену.\n"
        "Пример: «беру #12 у поставщика 3 за 85000»\n"
        "Или: /deal 12 3 85000"
    ),
    "alert_morning_price_degraded": (
        "[ALERT] Утренний прайс: ошибки при загрузке из каналов "
        "(degraded={degraded_count}). Проверьте парсер и каналы поставщиков."
    ),
    "alert_morning_price_empty": (
        "[ALERT] Утренний прайс: черновик не создан — нет позиций для публикации."
    ),
    "help": (
        "Доступные команды:\n"
        "/ask — запрос поставщикам (можно с reply на сообщение клиента)\n"
        "/bargain {id} {target_price} [supplier_id] — торг с поставщиком\n"
        "/recheck {id} [hours] — повторная проверка цены (2–5 ч, по умолчанию 3)\n"
        "/setprice {id} {supplier_id} {price} [qty] — ручная фиксация цены\n"
        "/deal {id} {supplier_id} {price} — закрыть сделку\n"
        "«беру #N у поставщика S за ЦЕНА» — закрыть сделку (reply/@бот)\n"
        "/status {id} — статус заявки и предложения\n"
        "/cancel {id} — отменить заявку\n"
        "/approve_price {draft_id} — утвердить черновик прайса\n"
        "/reject_price {draft_id} — отклонить черновик прайса\n"
        "/help — эта справка"
    ),
}


def _format_field(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _format_available(value: Any) -> str:
    if value is True:
        return "есть"
    if value is False:
        return "нет"
    return "—"


def _format_price(value: Any) -> str:
    if value is None:
        return "—"
    return f"{value} ₽"


def _format_qty(value: Any) -> str:
    if value is None:
        return "—"
    return str(value)


def render_template(name: str, **kwargs: Any) -> str:
    """Render a named template with safe defaults for optional SKU fields."""
    if name not in TEMPLATES:
        raise KeyError(f"Unknown template: {name}")

    normalized = kwargs.pop("normalized_json", None) or {}
    if name == "ask":
        kwargs.setdefault("request_id", kwargs.get("request_id", ""))
        for field in ("model", "storage", "color", "region", "sim"):
            kwargs.setdefault(field, _format_field(normalized.get(field)))
        qty = normalized.get("qty")
        kwargs.setdefault("qty", str(qty) if qty is not None else "—")

    if name == "supplier_quote_parsed":
        kwargs.setdefault("available_text", _format_available(kwargs.pop("available", None)))
        kwargs.setdefault("price_text", _format_price(kwargs.pop("price", None)))
        kwargs.setdefault("qty_text", _format_qty(kwargs.pop("qty", None)))

    if name == "status_summary":
        kwargs.setdefault("quotes_block", kwargs.get("quotes_block") or "—")
        kwargs.setdefault("deal_block", kwargs.get("deal_block") or "")

    if name in {"price_draft", "price_list_published"}:
        kwargs.setdefault("items_block", kwargs.get("items_block") or "—")

    return TEMPLATES[name].format(**kwargs)
