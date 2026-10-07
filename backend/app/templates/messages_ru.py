"""Pre-defined outgoing message templates (TECH DOC §10)."""

from __future__ import annotations

from typing import Any

TEMPLATES: dict[str, str] = {
    "ask": (
        "Запрос #{request_id}: {source_text}\n"
        "Ответьте ценой. Если открыто несколько запросов, укажите номер: "
        "#{request_id} 117900."
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
    "ask_sent_multi": "Запросы {request_ids} отправлены ({N} доставок поставщикам)",
    "ask_status_card": (
        "Заявка #{request_id}: отправлено {sent_count}, ответили {replied_count}, "
        "цен {quoted_count}, требуют разбора {pending_review_count}."
    ),
    "supplier_review_card": (
        "Неразобранный ответ\n"
        "Поставщик: {supplier_name}\n"
        "{raw_line}\n"
        "Цена: {price_line}\n"
        "Атрибуты: {attrs_line}\n"
        "Конфликт: {conflict_line}\n"
        "Заявки: {candidates_line}\n"
        "Возраст: {age_minutes} мин"
    ),
    "supplier_price_corrected_employee": (
        "Заявка #{request_id}: поставщик исправил цену {old_price} → {new_price} "
        "(клиенту: {client_old} → {client_new})"
    ),
    "ask_no_suppliers": (
        "Заявка #{request_id}: нет поставщиков для категории «{category}». "
        "Оператор уведомлён."
    ),
    "alert_suppliers_without_categories": (
        "Поставщики без категорий (рассылка пропущена): {names}"
    ),
    "alert_unknown_product_category": (
        "Заявка #{request_id}: не удалось определить категорию товара.\n{source_text}"
    ),
    "alert_no_suppliers_for_category": (
        "Заявка #{request_id}, категория {category}: нет подходящих поставщиков.\n"
        "{source_text}"
    ),
    "alert_unbound_supplier_message": (
        "Поставщик {supplier_label} прислал сообщение без привязки к заявке:\n"
        "{raw_text}\n\n"
        "К какой заявке отнести?\n{candidates_block}"
    ),
    "alert_auto_bound_supplier_price": (
        "Поставщик {supplier_label} прислал цену без указания заявки:\n"
        "{raw_text}\n\n"
        "Отнёс к заявке #{request_id}: {request_label}\n"
        "Клиенту отправлено: {final_price} (цена поставщика {supplier_price})\n\n"
        "Если заявка не та — нажмите правильную:"
    ),
    "client_quote_withdrawn": (
        "Заявка #{request_id}: цена уточняется, предыдущее предложение неактуально."
    ),
    "markup_rules_list": "Наценки. Нажмите правило, чтобы изменить сумму:\n{rules_block}",
    "markup_edit_prompt": "Наценка «{rule_label}» сейчас {amount} ₽. Выберите сумму или напишите свою.",
    "markup_enter_amount": "Напишите новую наценку для «{rule_label}» числом, например 800.",
    "markup_rule_updated": "Наценка {rule_key} = {amount} ₽",
    "markup_rule_not_found": "Правило {rule_key} не найдено",
    "supplier_categories_list": "Категории поставщиков:\n{lines}",
    "supplier_category_updated": "Поставщик #{supplier_id}: категории {categories}",
    "bind_ok": "Сообщение привязано к заявке #{request_id}",
    "bind_already": "Сообщение уже привязано к заявке #{request_id}",
    "bind_failed": "Не удалось привязать: {reason}",
    "bind_unbound_ok": "Привязка снята, клиенту отправлена поправка",
    "bind_ignored_ok": "Сообщение отмечено как игнорируемое",
    "supplier_need_reply": (
        "Пожалуйста, ответьте на сообщение с номером заявки (#N)"
    ),
    "supplier_bind_confirm": "Это на {request_text}?",
    "supplier_bind_ambiguous": (
        "К какой заявке относится цена {price}?\n"
        "{candidate_lines}\n"
        "Нажмите кнопку или отправьте номер заявки: #{first_request_id}"
    ),
    "supplier_bind_gave_up": (
        "Не удалось привязать ответ к заявке. "
        "Ответьте reply на сообщение с номером заявки (#N)."
    ),
    "supplier_quote_parsed": (
        "По вашему запросу · Заявка #{request_id}\n"
        "Наличие: {available_text}\n"
        "Цена: {price_text}\n"
        "Кол-во: {qty_text}"
    ),
    "supplier_low_confidence": (
        "По вашему запросу · Заявка #{request_id}\n"
        "Цена: —\n"
        "Комментарий: {raw_text}"
    ),
    "deal_closed": (
        "Заявка #{request_id} закрыта. Поставщик: {supplier_label}. "
        "Цена: {final_price} ₽"
    ),
    "setprice_ok": (
        "Цена зафиксирована: заявка #{request_id}, поставщик {supplier_label}, "
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
    "purge_request_confirm": (
        "Заявка #{request_id} и все её сообщения/котировки/сделки "
        "будут безвозвратно удалены. Подтвердите?"
    ),
    "purge_request_ok": "Заявка #{request_id} удалена из БД.",
    "purge_request_not_found": "Заявка #{request_id} не найдена.",
    "purge_request_invalid": "Использование: /purge_request {id}",
    "purge_old_preview": (
        "Dry-run: будет удалено {count} заявок "
        "(cancelled/closed старше {days} дн.). ID: {ids}"
    ),
    "purge_old_done": "Удалено {count} заявок: {ids}",
    "purge_old_invalid": "Использование: /purge_old {days} [limit] [--confirm]",
    "purge_cancelled": "Удаление отменено.",
    "request_not_found": "Заявка #{request_id} не найдена",
    "supplier_not_found": "Поставщик #{supplier_id} не найден",
    "request_not_open": "Заявка #{request_id} уже закрыта или отменена",
    "deal_already_exists": "По заявке #{request_id} уже есть сделка",
    "invalid_command": "Неверный формат команды",
    "bargain_sent": (
        "Торг по заявке #{request_id}: запрошена цена {target_price} ₽ "
        "у поставщика {supplier_label}"
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
    "supplier_delivery_failed": (
        "Заявка #{request_id}: не доставлено ({kind}):\n{failed_block}"
    ),
    "admin_business_status": (
        "Telegram Business\n\n"
        "Подключения:\n{connections_block}\n\n"
        "Поставщики с business_dm:\n{suppliers_block}"
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
        "Вы владелец.\n"
        "Кнопки внизу открывают меню, отчёты и наценки и скрываются после нажатия. "
        "Команды писать не нужно."
    ),
    "employee_start_ok": (
        "Вы сотрудник.\n"
        "Кнопки внизу: новый запрос и список ваших заявок.\n"
        "Товар пишите обычным сообщением, после кнопки «Новый запрос»."
    ),
    "employee_ask_prompt": "Напишите товар и характеристики одним сообщением.",
    "employee_ask_pick_group": "В какую беседу отправить запрос?",
    "employee_no_client_group": (
        "Нет клиентской беседы. Попросите владельца привязать её в меню."
    ),
    "employee_requests_list": "Ваши открытые заявки:",
    "employee_requests_empty": "Открытых заявок нет.",
    "employee_price_prompt": (
        "Заявка #{request_id}. Напишите цену.\n"
        "Если поставщиков несколько: номер поставщика и цена, например 3 85000."
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
        "Работайте кнопками в личке с ботом.\n"
        "Сотрудник: «Новый запрос» и «Мои заявки».\n"
        "Владелец: «Меню», отчёты и наценки.\n"
        "Если кнопок нет, откройте бота заново и нажмите Start."
    ),
    # Owner admin menu
    "admin_main_menu": (
        "Администрирование бота\n\n"
        "Выбирайте раздел:"
    ),
    "admin_requests_list": "Заявки (новые сверху):",
    "admin_requests_empty": "Заявок пока нет.",
    "admin_request_detail": (
        "Заявка #{request_id}\n"
        "Статус: {status_label}\n"
        "Текст: {source_text}"
    ),
    "admin_request_purge_confirm": (
        "Удалить заявку #{request_id} навсегда? Сообщения и котировки тоже удалятся."
    ),
    "admin_request_already_done": (
        "Заявка #{request_id} уже закрыта или отменена."
    ),
    "admin_supplier_list": "Список поставщиков:",
    "admin_supplier_detail": (
        "Поставщик #{supplier_id} {supplier_name}\n"
        "Активен: {active}\n"
        "RFQ включён: {rfq_enabled}\n"
        "Telegram ID: {telegram_id}\n"
        "ЛС открыт (dm_ok): {dm_ok}\n"
        "Канал прайса: {price_channel_label}\n"
        "Категории заявок: {categories_label}"
    ),
    "admin_supplier_categories": (
        "Категории поставщика #{supplier_id} {supplier_name}\n\n"
        "Заявка уходит только по включённым категориям.\n"
        "Нажмите строку, чтобы включить или выключить."
    ),
    "admin_supplier_channel_pick": (
        "Выберите канал прайса для поставщика #{supplier_id}:\n"
        "⏳ — канал ещё не готов"
    ),
    "admin_channel_not_ready": (
        "Канал ещё не готов, подождите ✅\n"
        "Telegram ID канала появится после первой синхронизации парсера."
    ),
    "admin_channel_taken": (
        "Этот канал уже привязан к другому поставщику. "
        "Сначала отвяжите его там или выберите другой канал."
    ),
    "admin_channel_bound": (
        "Канал {channel_label} привязан к поставщику #{supplier_id}."
    ),
    "admin_channel_unbound": "Канал прайса отвязан от поставщика #{supplier_id}.",
    "admin_supplier_bind_prompt": (
        "Как привязать личный Telegram поставщика?\n"
        "↩️ Переслать сообщение — бот сам увидит ID\n"
        "🔗 Ссылка — поставщик откроет ссылку и нажмёт Start\n"
        "⏭ Пропустить — работа только через групповой чат"
    ),
    "admin_supplier_forward_prompt": (
        "Перешлите боту любое сообщение от поставщика. "
        "Если в настройках приватности ID скрыт, используйте ссылку."
    ),
    "admin_supplier_bind_link": (
        "Отправьте поставщику одноразовую ссылку:\n{link}\n\n"
        "После нажатия Start личка откроется автоматически."
    ),
    "admin_bind_link_unavailable": (
        "Ссылка временно недоступна: у бота не задан username в настройках.\n"
        "Используйте «Переслать сообщение» или добавьте username бота позже."
    ),
    "admin_supplier_forward_hidden": (
        "Не удалось определить ID отправителя — настройки приватности скрывают его.\n"
        "Создайте поставщику одноразовую ссылку кнопкой ниже."
    ),
    "admin_supplier_invalid_telegram_id": (
        "Не распознал ID. Перешлите сообщение от поставщика или попробуйте ссылку."
    ),
    "admin_supplier_telegram_id_busy": (
        "Этот Telegram ID уже занят другим поставщиком, сотрудником или владельцем. "
        "Попробуйте ссылку, если человек тот же."
    ),
    "admin_supplier_bound": (
        "Поставщик #{supplier_id} {supplier_name} привязан к Telegram ID {telegram_id}.\n"
        "Личные сообщения открыты."
    ),
    "admin_supplier_unbound": (
        "Личка отвязана от поставщика #{supplier_id} {supplier_name}.\n"
        "Групповой чат остаётся активным."
    ),
    "admin_bind_conflict_notice": (
        "Внимание: поставщик #{supplier_id} {supplier_name} пытался привязать "
        "Telegram ID {telegram_id}, но оно уже занято другим пользователем. "
        "Создайте новую ссылку, если нужно."
    ),
    "supplier_bind_conflict": (
        "Этот Telegram-аккаунт уже привязан к другому пользователю бота. "
        "Попросите заказчика отправить вам новую ссылку."
    ),
    "supplier_bind_token_invalid": (
        "Ссылка недействительна, уже использована или устарела. "
        "Попросите заказчика прислать новую."
    ),
    # Legacy templates kept for backwards compatibility with old admin callbacks
    "admin_supplier_tgid_prompt": (
        "Отправьте Telegram ID поставщика (только цифры из @userinfobot)\n"
        "или перешлите любое сообщение от этого человека."
    ),
    "admin_supplier_tgid_saved": (
        "Telegram ID {telegram_id} сохранён для поставщика #{supplier_id}.\n"
        "Поставщику нужно написать боту /start в ЛС, чтобы открыть личные сообщения."
    ),
    "admin_supplier_tgid_taken": (
        "Telegram ID {telegram_id} уже используется другим поставщиком или чатом. "
        "Введите другой ID."
    ),
    "admin_supplier_tgid_cleared": (
        "Telegram ID сброшен для поставщика #{supplier_id}.\n"
        "RFQ в личку не пойдёт, пока ID не задан снова и поставщик не нажмёт /start."
    ),
    "admin_supplier_chats": "Чаты поставщика #{supplier_id} (всего {count}):",
    "admin_client_groups": "Клиентские беседы:",
    "admin_pending_chats": "Новые чаты, куда добавили бота:",
    "admin_bind_supplier": "Выберите поставщика для этой беседы:",
    "admin_price_channels": "Каналы прайсов поставщиков:",
    "admin_price_channels_error": "Не удалось загрузить каналы: {detail}",
    "admin_await_supplier_name": "Введите название нового поставщика:",
    "admin_await_rename": "Введите новое название поставщика:",
    "admin_await_channel_handle": (
        "Введите username или ID канала с прайсами:\n"
        "Пример: @supplier_prices или -1001234567890"
    ),
    "admin_need_name": "Название не может быть пустым. Введите ещё раз:",
    "admin_need_channel": "Канал не может быть пустым. Введите ещё раз:",
    "admin_channel_add_error": "Не удалось добавить канал: {detail}",
    "admin_employees_list": "Список сотрудников:",
    "admin_employee_detail": (
        "Сотрудник #{employee_id} {employee_name}\n"
        "Telegram ID: {telegram_id}\n"
        "Активен: {active}"
    ),
    "admin_await_employee_name": "Введите имя нового сотрудника:",
    "admin_await_employee_telegram_id": (
        "Отправьте Telegram ID сотрудника (только цифры из @userinfobot)\n"
        "или перешлите любое сообщение от этого человека."
    ),
    "admin_employee_need_name": "Имя не может быть пустым. Введите ещё раз:",
    "admin_employee_invalid_telegram_id": (
        "Некорректный Telegram ID. Введите только цифры из @userinfobot "
        "или перешлите сообщение сотрудника."
    ),
    "admin_employee_duplicate_telegram_id": (
        "Сотрудник с Telegram ID {telegram_id} уже есть в базе. "
        "Введите другой ID или отмените добавление."
    ),
    "admin_employee_forward_hidden": (
        "Не удалось определить ID отправителя (скрыт настройками приватности). "
        "Пришлите числовой ID из @userinfobot."
    ),
    "admin_employee_delete_confirm": (
        "Удалить сотрудника #{employee_id} {employee_name}?"
    ),
    "admin_employee_deleted": "Сотрудник #{employee_id} удалён.",
    "admin_employee_has_requests": (
        "Нельзя удалить сотрудника #{employee_id}: на нём {count} заявок. "
        "Сначала удалите заявки в разделе «Заявки»."
    ),
    "admin_supplier_delete_confirm": (
        "Удалить поставщика #{supplier_id} {supplier_name}? "
        "Его чаты, котировки и прайсы тоже удалятся."
    ),
    "admin_supplier_deleted": "Поставщик #{supplier_id} удалён.",
    "admin_dialog_expired": "Сессия устарела. Начните сначала через /menu.",
    "admin_unknown_command": "Неизвестная команда. Используйте /menu",
    "admin_error": "Ошибка: {detail}",
    "llm_billing_reminder": (
        "Напоминание: пополните баланс {provider}.\n"
        "Ориентировочная стоимость: {price_text}.\n"
        "Оплата: {payment_url}"
    ),
    "llm_price_updated": "Цена LLM обновлена:\n{price_text}",
    "llm_price_usage": "Использование: /set_llm_price <текст цены>\nТекущая: {price_text}",
    "admin_chat_added": "Беседа добавлена как «{role}». Можно поменять в /menu → Новые чаты.",
    "admin_chat_conflict": "Чат уже привязан как {role}. Сначала отвяжите в /menu.",
    "admin_chat_bot_removed": "Бота удалили из беседы {chat_id}. Она деактивирована.",
    "admin_chat_classify_prompt": (
        "Бота добавили в беседу «{title}».\n"
        "Как её использовать?"
    ),
}


_SUPPLIER_LABEL_TEMPLATES = frozenset(
    {
        "deal_closed",
        "setprice_ok",
        "bargain_sent",
        "alert_unbound_supplier_message",
        "alert_auto_bound_supplier_price",
    }
)


def format_supplier_label(name: str | None, supplier_id: int) -> str:
    """Format the unified supplier label: ``{name} (#{id})`` or ``#{id}`` if no name."""
    if name:
        return f"{name} (#{supplier_id})"
    return f"#{supplier_id}"


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

    kwargs.pop("normalized_json", None)
    if name in _SUPPLIER_LABEL_TEMPLATES:
        supplier_name = kwargs.pop("supplier_name", None)
        supplier_id = kwargs.pop("supplier_id")
        kwargs["supplier_label"] = format_supplier_label(supplier_name, supplier_id)

    if name == "ask":
        kwargs.setdefault("request_id", kwargs.get("request_id", ""))
        kwargs.setdefault("source_text", (kwargs.get("source_text") or "").strip())

    if name == "supplier_quote_parsed":
        kwargs.setdefault("available_text", _format_available(kwargs.pop("available", None)))
        kwargs.setdefault("price_text", _format_price(kwargs.pop("price", None)))
        kwargs.setdefault("qty_text", _format_qty(kwargs.pop("qty", None)))

    if name == "status_summary":
        kwargs.setdefault("quotes_block", kwargs.get("quotes_block") or "—")
        kwargs.setdefault("deal_block", kwargs.get("deal_block") or "")

    if name in {"price_draft", "price_list_published"}:
        kwargs.setdefault("items_block", kwargs.get("items_block") or "—")

    if name == "supplier_delivery_failed":
        kwargs.setdefault("failed_block", kwargs.get("failed_block") or "—")
        kwargs.setdefault("kind", kwargs.get("kind") or "ask")

    if name == "admin_business_status":
        kwargs.setdefault("connections_block", kwargs.get("connections_block") or "нет")
        kwargs.setdefault("suppliers_block", kwargs.get("suppliers_block") or "нет")

    if name == "admin_supplier_detail":
        kwargs.setdefault("telegram_id", "—")
        kwargs.setdefault("dm_ok", "—")
        kwargs.setdefault("price_channel_label", "не привязан")
        kwargs.setdefault("categories_label", "—")

    return TEMPLATES[name].format(**kwargs)
