"""Owner/admin inline menu for managing suppliers, chats, groups, channels.

All DB mutations are delegated to admin_service.py; this module only handles
Telegram UI and callback data.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import (
    Employee,
    PendingChat,
    PriceListDraft,
    ProductCategory,
    RequestStatus,
    Supplier,
)
from app.services import admin_service, parser_client, purge_service
from app.services.deal_service import (
    RequestNotFoundError,
    RequestNotOpenError,
    cancel_request,
)
from app.services.request_card_service import render_admin_request_detail
from app.telegram.client import TelegramClientProtocol, get_telegram_client
from app.telegram.home_buttons import EMP_PENDING_REPLIES, OWNER_MENU
from app.telegram.keyboards import (
    CallbackData,
    button,
    inline_keyboard,
    menu_button,
    paginated_keyboard,
)
from app.templates.messages_ru import render_template
from app.utils.telegram import (
    extract_message_text,
    resolve_user_id_from_message,
)
from app.utils.whitelist import get_owner_by_telegram_id

_PAGE_SIZE = 8
_REQUEST_STATUS_LABELS = {
    RequestStatus.open: "новая",
    RequestStatus.awaiting_answers: "ждём ответы",
    RequestStatus.priced: "есть цены",
    RequestStatus.bargaining: "торг",
    RequestStatus.needs_recheck: "перепроверка",
    RequestStatus.closed: "закрыта",
    RequestStatus.cancelled: "отменена",
}
_MAX_REQUEST_SNIPPET = 48
_CATEGORY_ORDER: tuple[ProductCategory, ...] = (
    ProductCategory.apple,
    ProductCategory.samsung,
    ProductCategory.power_station,
    ProductCategory.other,
)
_CATEGORY_LABELS = {
    ProductCategory.apple: "Apple",
    ProductCategory.samsung: "Samsung",
    ProductCategory.power_station: "Электростанции",
    ProductCategory.other: "Прочее",
}


def _cb(action: str, arg: int = 0, page: int = 0) -> CallbackData:
    return CallbackData(namespace="admin", action=action, arg=arg, page=page)


async def _send_or_edit(
    telegram: TelegramClientProtocol,
    *,
    chat_id: int,
    text: str,
    message_id: int | None = None,
    markup: dict[str, Any] | None = None,
) -> None:
    if message_id is not None:
        try:
            await telegram.edit_message_text(
                chat_id, message_id, text, reply_markup=markup
            )
            return
        except Exception:
            pass
    await telegram.send_message(chat_id, text, reply_markup=markup)


async def handle_admin_message(
    session: AsyncSession,
    message: dict[str, Any],
    *,
    telegram: TelegramClientProtocol | None = None,
) -> str:
    telegram = telegram or get_telegram_client()
    from_user = message.get("from") or {}
    owner_id = int(from_user.get("id", 0))
    chat_id = int((message.get("chat") or {}).get("id", 0))
    text = extract_message_text(message)

    owner = await get_owner_by_telegram_id(session, owner_id)
    if owner is None:
        return "ignored"

    dialog = await admin_service.get_dialog(session, owner_id)
    if dialog is not None:
        return await _handle_dialog_text(
            session, dialog, owner_id, chat_id, message, telegram
        )

    if text == EMP_PENDING_REPLIES:
        from app.handlers.employee_inbox import send_pending_inbox

        await send_pending_inbox(
            session,
            chat_id=chat_id,
            telegram=telegram,
            employee=None,
            is_owner=True,
        )
        return "ok"

    if text == OWNER_MENU or text.startswith("/menu") or text.startswith("/admin") or text.startswith("/"):
        await _send_main_menu(session, telegram, chat_id)
        return "ok"

    await telegram.send_message(chat_id, render_template("admin_unknown_command"))
    return "ok"


async def handle_admin_callback(
    session: AsyncSession,
    callback_query: dict[str, Any],
) -> str:
    telegram = get_telegram_client()
    from_user = callback_query.get("from") or {}
    owner_id = int(from_user.get("id", 0))
    callback_id = callback_query.get("id", "")

    owner = await get_owner_by_telegram_id(session, owner_id)
    if owner is None:
        await telegram.answer_callback_query(callback_id, text="Нет доступа")
        return "ignored"

    await telegram.answer_callback_query(callback_id)

    message = callback_query.get("message") or {}
    chat_id = int((message.get("chat") or {}).get("id", 0))
    message_id = message.get("message_id")

    raw_data = callback_query.get("data")
    cd = CallbackData.decode(raw_data)
    if cd is None or cd.namespace != "admin":
        return "ignored"

    try:
        await _dispatch_callback(session, cd, owner_id, chat_id, message_id, telegram)
    except Exception as exc:
        logger.warning("Admin callback failed owner_id={} cd={}: {}", owner_id, cd, exc)
        await _send_or_edit(
            telegram,
            chat_id=chat_id,
            text=render_template("admin_error", detail="операция не выполнена"),
            message_id=message_id,
            markup=inline_keyboard([[menu_button("В меню", "main_menu")]]),
        )
        return "error"
    return "ok"


async def _dispatch_callback(
    session: AsyncSession,
    cd: CallbackData,
    owner_id: int,
    chat_id: int,
    message_id: int | None,
    telegram: TelegramClientProtocol,
) -> None:
    action = cd.action

    if action == "main_menu":
        await _send_main_menu(session, telegram, chat_id, message_id=message_id)
        return

    if action == "dlq":
        await _send_dlq_list(session, telegram, chat_id, message_id=message_id)
        return

    if action in ("dlqin", "dlqout"):
        await _send_dlq_item(
            session,
            telegram,
            chat_id,
            queue="inbox" if action == "dlqin" else "outbox",
            row_id=cd.arg,
            message_id=message_id,
        )
        return

    if action == "dlqin_go":
        from app.services.dlq_service import replay_inbox

        outcome = await replay_inbox(session, cd.arg, actor_telegram_id=owner_id)
        await _send_or_edit(
            telegram,
            chat_id=chat_id,
            text="Повтор поставлен в очередь." if outcome == "replayed" else "Строка уже закрыта.",
            message_id=message_id,
            markup=inline_keyboard([[menu_button("К ошибкам", "dlq")]]),
        )
        return

    if action == "dlqout_go":
        from app.services.dlq_service import ReplayNeedsConfirmation, replay_outbox

        try:
            outcome = await replay_outbox(
                session,
                cd.arg,
                actor_telegram_id=owner_id,
                confirmed=True,
            )
        except ReplayNeedsConfirmation:
            outcome = "confirm"
        await _send_or_edit(
            telegram,
            chat_id=chat_id,
            text="Повтор поставлен в очередь." if outcome == "replayed" else "Строка уже закрыта.",
            message_id=message_id,
            markup=inline_keyboard([[menu_button("К ошибкам", "dlq")]]),
        )
        return

    if action in ("dlqin_ok", "dlqout_ok"):
        from app.services.dlq_service import resolve_row

        outcome = await resolve_row(
            session,
            queue="inbox" if action == "dlqin_ok" else "outbox",
            row_id=cd.arg,
            actor_telegram_id=owner_id,
            reason="operator_resolved",
        )
        await _send_or_edit(
            telegram,
            chat_id=chat_id,
            text="Закрыто без повтора." if outcome == "resolved" else "Строка уже закрыта.",
            message_id=message_id,
            markup=inline_keyboard([[menu_button("К ошибкам", "dlq")]]),
        )
        return

    if action == "report_day":
        from app.handlers.owner_commands import send_owner_report

        await send_owner_report(session, chat_id=chat_id, period="day", telegram=telegram)
        return

    if action == "report_week":
        from app.handlers.owner_commands import send_owner_report

        await send_owner_report(session, chat_id=chat_id, period="week", telegram=telegram)
        return

    if action == "report_purchases":
        from app.handlers.purchase_report_commands import send_purchase_report

        await send_purchase_report(
            session,
            chat_id=chat_id,
            telegram=telegram,
            period="day",
            message_id=message_id,
        )
        return

    if action == "report_purchases_yest":
        from app.handlers.purchase_report_commands import send_purchase_report

        settings = get_settings()
        yesterday = datetime.now(ZoneInfo(settings.tz)).date() - timedelta(days=1)
        await send_purchase_report(
            session,
            chat_id=chat_id,
            telegram=telegram,
            period="day",
            on_day=yesterday,
            message_id=message_id,
        )
        return

    if action == "report_purchases_week":
        from app.handlers.purchase_report_commands import send_purchase_report

        await send_purchase_report(
            session,
            chat_id=chat_id,
            telegram=telegram,
            period="week",
            message_id=message_id,
        )
        return

    if action == "markup_list":
        from app.handlers.owner_commands import send_markup_list

        await send_markup_list(session, chat_id=chat_id, telegram=telegram)
        return

    if action == "mk_add":
        await admin_service.set_dialog(
            session,
            telegram_id=owner_id,
            state="await_markup_rule_phrase",
            payload={},
        )
        await telegram.send_message(
            chat_id,
            render_template("markup_add_enter_phrase"),
        )
        return

    if action == "mk_open":
        from app.handlers.owner_commands import send_markup_edit

        await send_markup_edit(
            session, chat_id=chat_id, rule_id=cd.arg, telegram=telegram
        )
        return

    if action == "mk_skip":
        from app.handlers.owner_commands import decline_markup_prompt

        await decline_markup_prompt(
            session,
            chat_id=chat_id,
            rule_id=cd.arg,
            telegram=telegram,
        )
        return

    if action == "mk_set":
        from decimal import Decimal

        from app.handlers.owner_commands import apply_markup_choice

        await apply_markup_choice(
            session,
            chat_id=chat_id,
            rule_id=cd.arg,
            amount=Decimal(cd.page),
            telegram=telegram,
        )
        return

    if action == "mk_custom":
        await admin_service.set_dialog(
            session,
            telegram_id=owner_id,
            state="await_markup_amount",
            payload={"rule_id": cd.arg},
        )
        from app.db.models import MarkupRule

        rule = await session.get(MarkupRule, cd.arg)
        label = (rule.rule_key if rule and rule.rule_key else "правило").replace("_", " ")
        await telegram.send_message(
            chat_id,
            render_template("markup_enter_amount", rule_label=label),
        )
        return

    if action == "bind_pick":
        from app.handlers.supplier_messages import manual_bind_message

        result = await manual_bind_message(
            session,
            message_in_id=cd.arg,
            request_id=cd.page,
            telegram=telegram,
        )
        if result == "ok":
            text = render_template("bind_ok", request_id=cd.page)
        elif result == "already_bound":
            text = render_template("bind_already", request_id=cd.page)
        else:
            text = render_template("bind_failed", reason=result)
        await _send_or_edit(
            telegram, chat_id=chat_id, text=text, message_id=message_id
        )
        return

    if action == "bind_unbind":
        from app.handlers.supplier_messages import unbind_message

        result = await unbind_message(session, message_in_id=cd.arg, telegram=telegram)
        text = (
            render_template("bind_unbound_ok")
            if result == "ok"
            else render_template("bind_failed", reason=result)
        )
        await _send_or_edit(
            telegram, chat_id=chat_id, text=text, message_id=message_id
        )
        return

    if action == "bind_ignore":
        from app.handlers.supplier_messages import unbind_message

        result = await unbind_message(
            session, message_in_id=cd.arg, telegram=telegram, status="ignored"
        )
        text = (
            render_template("bind_ignored_ok")
            if result == "ok"
            else render_template("bind_failed", reason=result)
        )
        await _send_or_edit(
            telegram, chat_id=chat_id, text=text, message_id=message_id
        )
        return

    if action == "bind_stop":
        try:
            await cancel_request(session, request_id=cd.arg)
            text = render_template("bind_stop_ok", request_id=cd.arg)
        except RequestNotFoundError:
            text = render_template("purge_request_not_found", request_id=cd.arg)
        except RequestNotOpenError:
            text = render_template(
                "admin_request_already_done", request_id=cd.arg
            )
        await _send_or_edit(
            telegram, chat_id=chat_id, text=text, message_id=message_id
        )
        return

    if action == "requests":
        await _send_request_list(session, telegram, chat_id, cd.page, message_id=message_id)
        return

    if action == "req_detail":
        await _send_request_detail(
            session, telegram, chat_id, cd.arg, list_page=cd.page, message_id=message_id
        )
        return

    if action == "req_cancel":
        try:
            await cancel_request(session, request_id=cd.arg)
        except RequestNotFoundError:
            await _send_or_edit(
                telegram,
                chat_id=chat_id,
                text=render_template("purge_request_not_found", request_id=cd.arg),
                message_id=message_id,
                markup=inline_keyboard([[menu_button("К заявкам", "requests")]]),
            )
            return
        except RequestNotOpenError:
            await _send_or_edit(
                telegram,
                chat_id=chat_id,
                text=render_template("admin_request_already_done", request_id=cd.arg),
                message_id=message_id,
                markup=inline_keyboard(
                    [[menu_button("К заявке", "req_detail", cd.arg, cd.page)]]
                ),
            )
            return
        await _send_request_detail(
            session, telegram, chat_id, cd.arg, list_page=cd.page, message_id=message_id
        )
        return

    if action == "req_purge_ask":
        await _send_or_edit(
            telegram,
            chat_id=chat_id,
            text=render_template("admin_request_purge_confirm", request_id=cd.arg),
            message_id=message_id,
            markup=inline_keyboard(
                [
                    [menu_button("Удалить", "req_purge", cd.arg, cd.page)],
                    [menu_button("Отмена", "req_detail", cd.arg, cd.page)],
                ]
            ),
        )
        return

    if action == "req_purge":
        deleted = await purge_service.hard_delete_request(session, cd.arg)
        text = render_template(
            "purge_request_ok" if deleted else "purge_request_not_found",
            request_id=cd.arg,
        )
        await _send_or_edit(
            telegram,
            chat_id=chat_id,
            text=text,
            message_id=message_id,
            markup=inline_keyboard([[menu_button("К заявкам", "requests", page=cd.page)]]),
        )
        return

    if action == "business_status":
        await _send_business_status(session, telegram, chat_id, message_id=message_id)
        return

    if action == "suppliers":
        await _send_supplier_list(session, telegram, chat_id, cd.page, message_id=message_id)
        return

    if action == "employees":
        await _send_employee_list(session, telegram, chat_id, cd.page, message_id=message_id)
        return

    if action == "employee_detail":
        await _send_employee_detail(session, telegram, chat_id, cd.arg, message_id=message_id)
        return

    if action == "employee_add_start":
        await admin_service.set_dialog(
            session, telegram_id=owner_id, state="await_employee_name", payload={}
        )
        text = render_template("admin_await_employee_name")
        await _send_or_edit(
            telegram,
            chat_id=chat_id,
            text=text,
            message_id=message_id,
            markup=inline_keyboard([[menu_button("Отмена", "employees", page=0)]]),
        )
        return

    if action == "employee_toggle_active":
        await admin_service.toggle_employee_active(session, cd.arg)
        await _send_employee_detail(session, telegram, chat_id, cd.arg, message_id=message_id)
        return

    if action == "emp_del_ask":
        employee = await session.get(Employee, cd.arg)
        if employee is None:
            await _send_employee_list(session, telegram, chat_id, 0, message_id=message_id)
            return
        await _send_or_edit(
            telegram,
            chat_id=chat_id,
            text=render_template(
                "admin_employee_delete_confirm",
                employee_id=employee.id,
                employee_name=employee.name or "—",
            ),
            message_id=message_id,
            markup=inline_keyboard(
                [
                    [menu_button("Удалить", "emp_del", employee.id)],
                    [menu_button("Отмена", "employee_detail", employee.id)],
                ]
            ),
        )
        return

    if action == "emp_del":
        try:
            employee = await admin_service.delete_employee(session, cd.arg)
        except admin_service.EmployeeNotFoundError:
            await _send_employee_list(session, telegram, chat_id, 0, message_id=message_id)
            return
        except admin_service.EmployeeHasRequestsError as exc:
            await _send_or_edit(
                telegram,
                chat_id=chat_id,
                text=render_template(
                    "admin_employee_has_requests",
                    employee_id=exc.employee_id,
                    count=exc.request_count,
                ),
                message_id=message_id,
                markup=inline_keyboard(
                    [[menu_button("К сотруднику", "employee_detail", cd.arg)]]
                ),
            )
            return
        await _send_or_edit(
            telegram,
            chat_id=chat_id,
            text=render_template("admin_employee_deleted", employee_id=employee.id),
            message_id=message_id,
            markup=inline_keyboard([[menu_button("К сотрудникам", "employees")]]),
        )
        return

    if action == "supplier_detail":
        await _send_supplier_detail(session, telegram, chat_id, cd.arg, message_id=message_id)
        return

    if action == "supplier_add_start":
        await admin_service.set_dialog(
            session, telegram_id=owner_id, state="await_supplier_name", payload={}
        )
        text = render_template("admin_await_supplier_name")
        await _send_or_edit(
            telegram,
            chat_id=chat_id,
            text=text,
            message_id=message_id,
            markup=inline_keyboard([[menu_button("Отмена", "main_menu")]]),
        )
        return

    if action == "supplier_rename_start":
        await admin_service.set_dialog(
            session,
            telegram_id=owner_id,
            state="await_rename",
            payload={"supplier_id": cd.arg},
        )
        text = render_template("admin_await_rename")
        await _send_or_edit(
            telegram,
            chat_id=chat_id,
            text=text,
            message_id=message_id,
            markup=inline_keyboard([[menu_button("Отмена", "supplier_detail", cd.arg)]]),
        )
        return

    if action == "supplier_toggle_active":
        await admin_service.toggle_supplier_active(session, cd.arg)
        await _send_supplier_detail(session, telegram, chat_id, cd.arg, message_id=message_id)
        return

    if action == "supplier_toggle_rfq":
        await admin_service.toggle_supplier_rfq(session, cd.arg)
        await _send_supplier_detail(session, telegram, chat_id, cd.arg, message_id=message_id)
        return

    if action == "sup_cat":
        await _send_supplier_categories(
            session, telegram, chat_id, cd.arg, message_id=message_id
        )
        return

    if action == "sup_cat_tog":
        if 0 <= cd.page < len(_CATEGORY_ORDER):
            category = _CATEGORY_ORDER[cd.page]
            try:
                await admin_service.toggle_supplier_category(
                    session, cd.arg, category.value
                )
            except admin_service.SupplierNotFoundError:
                await _send_supplier_list(session, telegram, chat_id, 0, message_id=message_id)
                return
        await _send_supplier_categories(
            session, telegram, chat_id, cd.arg, message_id=message_id
        )
        return

    if action == "sup_del_ask":
        supplier = await session.get(Supplier, cd.arg)
        if supplier is None:
            await _send_supplier_list(session, telegram, chat_id, 0, message_id=message_id)
            return
        await _send_or_edit(
            telegram,
            chat_id=chat_id,
            text=render_template(
                "admin_supplier_delete_confirm",
                supplier_id=supplier.id,
                supplier_name=supplier.name or "—",
            ),
            message_id=message_id,
            markup=inline_keyboard(
                [
                    [menu_button("Удалить", "sup_del", supplier.id)],
                    [menu_button("Отмена", "supplier_detail", supplier.id)],
                ]
            ),
        )
        return

    if action == "sup_del":
        try:
            supplier = await admin_service.delete_supplier(session, cd.arg)
        except admin_service.SupplierNotFoundError:
            await _send_supplier_list(session, telegram, chat_id, 0, message_id=message_id)
            return
        await _send_or_edit(
            telegram,
            chat_id=chat_id,
            text=render_template("admin_supplier_deleted", supplier_id=supplier.id),
            message_id=message_id,
            markup=inline_keyboard([[menu_button("К поставщикам", "suppliers")]]),
        )
        return

    if action == "supplier_chats":
        await _send_supplier_chats(
            session, telegram, chat_id, cd.arg, message_id=message_id
        )
        return

    if action == "supplier_channel":
        await _send_supplier_channel_list(
            session, telegram, chat_id, cd.arg, message_id=message_id
        )
        return

    if action == "supplier_bind_ch":
        supplier_id = cd.arg
        parser_pk = cd.page
        try:
            channels = await parser_client.list_channels()
        except parser_client.ParserClientError as exc:
            await _send_or_edit(
                telegram,
                chat_id=chat_id,
                text=render_template("admin_price_channels_error", detail=str(exc)),
                message_id=message_id,
                markup=inline_keyboard(
                    [[menu_button("К поставщику", "supplier_detail", supplier_id)]]
                ),
            )
            return
        channel = next((ch for ch in channels if ch.id == parser_pk), None)
        if (
            channel is None
            or channel.channel_id is None
            or not channel.is_active
            or channel.status != "active"
        ):
            await _send_or_edit(
                telegram,
                chat_id=chat_id,
                text=render_template("admin_channel_not_ready"),
                message_id=message_id,
                markup=inline_keyboard(
                    [[menu_button("К каналам", "supplier_channel", supplier_id)]]
                ),
            )
            return
        try:
            await admin_service.bind_supplier_price_channel(
                session,
                supplier_id=supplier_id,
                channel_id=int(channel.channel_id),
                username=channel.username or channel.title,
            )
        except admin_service.PriceChannelBusyError:
            await _send_or_edit(
                telegram,
                chat_id=chat_id,
                text=render_template("admin_channel_taken"),
                message_id=message_id,
                markup=inline_keyboard(
                    [[menu_button("К каналам", "supplier_channel", supplier_id)]]
                ),
            )
            return
        await _send_supplier_detail(
            session, telegram, chat_id, supplier_id, message_id=message_id
        )
        return

    if action == "supplier_unbind_ch":
        await admin_service.unbind_supplier_price_channel(session, cd.arg)
        await _send_supplier_detail(
            session, telegram, chat_id, cd.arg, message_id=message_id
        )
        return

    if action == "supplier_tgid_start":
        await admin_service.set_dialog(
            session,
            telegram_id=owner_id,
            state="await_supplier_telegram_id",
            payload={"supplier_id": cd.arg},
        )
        await _send_or_edit(
            telegram,
            chat_id=chat_id,
            text=render_template("admin_supplier_tgid_prompt"),
            message_id=message_id,
            markup=inline_keyboard(
                [[menu_button("Отмена", "supplier_detail", cd.arg)]]
            ),
        )
        return

    if action == "supplier_tgid_clear":
        await admin_service.unbind_supplier_telegram_id(session, cd.arg)
        await _send_supplier_detail(
            session, telegram, chat_id, cd.arg, message_id=message_id
        )
        return

    if action == "supplier_bind_start":
        await admin_service.set_dialog(
            session,
            telegram_id=owner_id,
            state="await_supplier_bind",
            payload={"supplier_id": cd.arg},
        )
        await _send_or_edit(
            telegram,
            chat_id=chat_id,
            text=render_template("admin_supplier_bind_prompt"),
            message_id=message_id,
            markup=inline_keyboard(
                [
                    [
                        menu_button("↩️ Переслать сообщение", "supplier_bind_forward", cd.arg),
                        menu_button("🔗 Ссылка", "supplier_bind_link", cd.arg),
                    ],
                    [menu_button("⏭ Пропустить", "supplier_bind_skip", cd.arg)],
                    [menu_button("Отмена", "supplier_detail", cd.arg)],
                ]
            ),
        )
        return

    if action == "supplier_bind_forward":
        await admin_service.set_dialog(
            session,
            telegram_id=owner_id,
            state="await_supplier_forward",
            payload={"supplier_id": cd.arg},
        )
        await _send_or_edit(
            telegram,
            chat_id=chat_id,
            text=render_template("admin_supplier_forward_prompt"),
            message_id=message_id,
            markup=inline_keyboard(
                [[menu_button("Отмена", "supplier_detail", cd.arg)]]
            ),
        )
        return

    if action == "supplier_bind_link":
        username = get_settings().telegram_bot_username
        if not username:
            await _send_or_edit(
                telegram,
                chat_id=chat_id,
                text=render_template("admin_bind_link_unavailable"),
                message_id=message_id,
                markup=inline_keyboard(
                    [[menu_button("↩️ Переслать сообщение", "supplier_bind_forward", cd.arg)],
                     [menu_button("Отмена", "supplier_detail", cd.arg)]]
                ),
            )
            return
        bind_token = await admin_service.create_supplier_bind_token(
            session, supplier_id=cd.arg, owner_id=owner_id
        )
        link = f"https://t.me/{username.lstrip('@')}?start={bind_token.token}"
        await _send_or_edit(
            telegram,
            chat_id=chat_id,
            text=render_template("admin_supplier_bind_link", link=link),
            message_id=message_id,
            markup=inline_keyboard(
                [
                    [menu_button("К поставщику", "supplier_detail", cd.arg)],
                    [menu_button("В меню", "main_menu")],
                ]
            ),
        )
        return

    if action == "supplier_bind_skip":
        await admin_service.clear_dialog(session, owner_id)
        await _send_supplier_detail(
            session, telegram, chat_id, cd.arg, message_id=message_id
        )
        return

    if action == "supplier_unbind":
        await admin_service.unbind_supplier_telegram_id(session, cd.arg)
        await _send_supplier_detail(
            session, telegram, chat_id, cd.arg, message_id=message_id
        )
        return

    if action == "supplier_set_default":
        await admin_service.set_supplier_default_chat(session, cd.arg, cd.page)
        await _send_supplier_chats(
            session, telegram, chat_id, cd.arg, message_id=message_id
        )
        return

    if action == "supplier_toggle_chat":
        await admin_service.toggle_chat_active(session, cd.arg, cd.page)
        await _send_supplier_chats(
            session, telegram, chat_id, cd.arg, message_id=message_id
        )
        return

    if action == "client_groups":
        await _send_client_groups(session, telegram, chat_id, message_id=message_id)
        return

    if action == "client_group_toggle":
        await admin_service.toggle_client_group(session, cd.arg)
        await _send_client_groups(session, telegram, chat_id, message_id=message_id)
        return

    if action == "pending_chats":
        await _send_pending_chats(session, telegram, chat_id, message_id=message_id)
        return

    if action == "pending_as_supplier":
        await _send_bind_supplier_list(
            session, telegram, chat_id, cd.arg, page=cd.page, message_id=message_id
        )
        return

    if action == "pending_as_client":
        await admin_service.bind_pending_chat_as_client_group(
            session,
            chat_id=cd.arg,
            title=None,
            bound_by_owner_id=owner_id,
        )
        await _send_pending_chats(session, telegram, chat_id, message_id=message_id)
        return

    if action == "pending_ignore":
        await admin_service.delete_pending_chat(session, cd.arg)
        await _send_pending_chats(session, telegram, chat_id, message_id=message_id)
        return

    if action == "bind_supplier_to_chat":
        await admin_service.bind_pending_chat_as_supplier(
            session,
            chat_id=cd.arg,
            supplier_id=cd.page,
            bound_by_owner_id=owner_id,
        )
        await _send_pending_chats(session, telegram, chat_id, message_id=message_id)
        return

    if action == "price_channels":
        await _send_price_channels(session, telegram, chat_id, message_id=message_id)
        return

    if action == "price_channel_add_start":
        await admin_service.set_dialog(
            session,
            telegram_id=owner_id,
            state="await_channel_handle",
            payload={},
        )
        text = render_template("admin_await_channel_handle")
        await _send_or_edit(
            telegram,
            chat_id=chat_id,
            text=text,
            message_id=message_id,
            markup=inline_keyboard([[menu_button("Отмена", "price_channels")]]),
        )
        return

    if action == "price_channel_delete":
        parser_pk = int(cd.arg)
        telegram_channel_id: int | None = None
        try:
            channels = await parser_client.list_channels()
            matched = next((ch for ch in channels if ch.id == parser_pk), None)
            if matched is not None and matched.channel_id is not None:
                telegram_channel_id = int(matched.channel_id)
            await parser_client.delete_channel(parser_pk)
        except parser_client.ParserClientError:
            await _send_or_edit(
                telegram,
                chat_id=chat_id,
                text=render_template("admin_price_channel_delete_error"),
                message_id=message_id,
                markup=inline_keyboard([[menu_button("К каналам", "price_channels")]]),
            )
            return
        unbound = 0
        if telegram_channel_id is not None:
            unbound = await admin_service.unbind_suppliers_by_price_channel(
                session, telegram_channel_id
            )
        notice = None
        if unbound:
            notice = render_template(
                "admin_price_channel_unbound",
                count=unbound,
            )
        await _send_price_channels(
            session,
            telegram,
            chat_id,
            message_id=message_id,
            notice=notice,
        )
        return

    if action == "price_channel_restore":
        parser_pk = int(cd.arg)
        try:
            channels = await parser_client.list_channels()
            matched = next((ch for ch in channels if ch.id == parser_pk), None)
            if matched is None:
                raise parser_client.ParserClientError("parser_channel_not_found")
            if matched.username:
                handle = f"@{matched.username}"
            elif matched.channel_id is not None:
                handle = str(matched.channel_id)
            else:
                handle = (matched.title or "").strip()
            if not handle:
                raise parser_client.ParserClientError("parser_channel_handle_missing")
            restored = await parser_client.add_channel(handle)
        except parser_client.ParserClientError:
            await _send_or_edit(
                telegram,
                chat_id=chat_id,
                text=render_template("admin_price_channel_restore_error"),
                message_id=message_id,
                markup=inline_keyboard([[menu_button("К каналам", "price_channels")]]),
            )
            return
        notice = render_template(
            "admin_price_channel_restored",
            label=str(restored.title or restored.username or restored.channel_id or handle),
        )
        await _send_price_channels(
            session,
            telegram,
            chat_id,
            message_id=message_id,
            notice=notice,
        )
        return

    if action == "parser":
        await _send_parser_screen(
            session,
            telegram,
            chat_id,
            tab=cd.arg,
            page=cd.page,
            message_id=message_id,
        )
        return

    if action == "parser_run":
        await _run_parser_morning_price(session, telegram, chat_id, cd.arg, message_id)
        return

    if action == "purge_confirm":
        deleted = await purge_service.hard_delete_request(session, cd.arg)
        text = render_template(
            "purge_request_ok" if deleted else "purge_request_not_found",
            request_id=cd.arg,
        )
        await _send_or_edit(
            telegram,
            chat_id=chat_id,
            text=text,
            message_id=message_id,
        )
        return

    if action == "purge_cancel":
        await _send_or_edit(
            telegram,
            chat_id=chat_id,
            text=render_template("purge_cancelled"),
            message_id=message_id,
        )
        return

    if action == "noop":
        return

    await _send_main_menu(session, telegram, chat_id, message_id=message_id)


async def _pending_count(session: AsyncSession) -> int:
    count = (await session.execute(select(func.count(PendingChat.chat_id)))).scalar_one()
    return int(count)


async def _send_dlq_list(
    session: AsyncSession,
    telegram: TelegramClientProtocol,
    chat_id: int,
    *,
    message_id: int | None = None,
) -> None:
    from app.services.dlq_service import list_open_failures

    data = await list_open_failures(session)
    lines = ["Ошибки очереди. Текст поставщика и секреты сюда не попадают."]
    rows: list[list[dict[str, Any]]] = []
    if not data["inbox"] and not data["outbox"]:
        lines.append("Открытых ошибок нет.")
    for item in data["inbox"]:
        lines.append(
            f"вход #{item['id']} попыток {item['attempts']} {item['last_error'] or ''}".rstrip()
        )
        rows.append([menu_button(f"Вход #{item['id']}", "dlqin", int(str(item["id"])))])
    for item in data["outbox"]:
        lines.append(
            f"исход #{item['id']} {item['status']} {item['kind']} "
            f"поставщик {item['supplier_id'] or '—'} {item['last_error'] or ''}".rstrip()
        )
        rows.append([menu_button(f"Исход #{item['id']}", "dlqout", int(str(item["id"])))])
    rows.append([menu_button("В меню", "main_menu")])
    await _send_or_edit(
        telegram,
        chat_id=chat_id,
        text="\n".join(lines)[:3500],
        message_id=message_id,
        markup=inline_keyboard(rows),
    )


async def _send_dlq_item(
    session: AsyncSession,
    telegram: TelegramClientProtocol,
    chat_id: int,
    *,
    queue: str,
    row_id: int,
    message_id: int | None,
) -> None:
    from app.services.dlq_service import list_open_failures

    data = await list_open_failures(session, limit=50)
    item = next((row for row in data[queue] if row["id"] == row_id), None)
    if item is None:
        await _send_dlq_list(session, telegram, chat_id, message_id=message_id)
        return
    chat_line = ""
    if queue == "outbox":
        chat_line = f"Чат: {item.get('chat_id') or '—'}\n"
    text = (
        f"{'Вход' if queue == 'inbox' else 'Исход'} #{item['id']}\n"
        f"Статус: {item['status']}\n"
        f"Тип: {item['kind']}\n"
        f"Попыток: {item['attempts']}\n"
        f"Заявка: {item['request_id'] or '—'}\n"
        f"Поставщик: {item['supplier_id'] or '—'}\n"
        f"{chat_line}"
        f"Ошибка: {item['last_error'] or '—'}\n"
        f"Время: {item['created_at']}"
    )
    go = "dlqin_go" if queue == "inbox" else "dlqout_go"
    ok = "dlqin_ok" if queue == "inbox" else "dlqout_ok"
    error_text = str(item.get("last_error") or "").lower()
    if queue == "outbox" and any(
        marker in error_text
        for marker in ("chat not found", "bot was kicked", "not a member")
    ):
        confirm = (
            "Бот не видит этот чат. Повтор туда же не дойдёт, пока бота "
            "не вернут в клиентскую группу заявки. Иначе закройте без повтора."
        )
    elif queue == "outbox":
        confirm = (
            "Повтор отправит сообщение ещё раз. Нажмите только если прошлый вызов не дошёл."
        )
    else:
        confirm = "Повтор снова обработает это обновление."
    await _send_or_edit(
        telegram,
        chat_id=chat_id,
        text=f"{text}\n\n{confirm}",
        message_id=message_id,
        markup=inline_keyboard(
            [
                [menu_button("Повторить", go, row_id)],
                [menu_button("Закрыть без повтора", ok, row_id)],
                [menu_button("К ошибкам", "dlq")],
            ]
        ),
    )


async def _send_main_menu(
    session: AsyncSession,
    telegram: TelegramClientProtocol,
    chat_id: int,
    *,
    message_id: int | None = None,
) -> None:
    count = await _pending_count(session)
    rows = [
        [menu_button("Заявки", "requests", page=0)],
        [menu_button("Поставщики", "suppliers", page=0)],
        [menu_button("Сотрудники", "employees", page=0)],
        [
            menu_button("Отчёт за день", "report_day"),
            menu_button("Отчёт за неделю", "report_week"),
        ],
        [menu_button("Закупки", "report_purchases")],
        [menu_button("Наценки", "markup_list")],
        [menu_button("Клиентские беседы", "client_groups")],
        [menu_button(f"Новые чаты ({count})", "pending_chats")],
        [menu_button("Каналы прайсов", "price_channels")],
        [menu_button("Парсер", "parser")],
        [menu_button("Business", "business_status")],
        [menu_button("Ошибки системы", "dlq")],
    ]
    await _send_or_edit(
        telegram,
        chat_id=chat_id,
        text=render_template("admin_main_menu"),
        message_id=message_id,
        markup=inline_keyboard(rows),
    )


def _request_snippet(source_text: str) -> str:
    text = " ".join((source_text or "").split())
    if len(text) <= _MAX_REQUEST_SNIPPET:
        return text or "—"
    return text[: _MAX_REQUEST_SNIPPET - 1] + "…"


def _request_status_label(status: RequestStatus) -> str:
    return _REQUEST_STATUS_LABELS.get(status, status.value)


def _request_is_open(status: RequestStatus) -> bool:
    return status not in (RequestStatus.closed, RequestStatus.cancelled)


async def _send_request_list(
    session: AsyncSession,
    telegram: TelegramClientProtocol,
    chat_id: int,
    page: int,
    *,
    message_id: int | None = None,
) -> None:
    requests = await admin_service.list_requests(session)
    if not requests:
        await _send_or_edit(
            telegram,
            chat_id=chat_id,
            text=render_template("admin_requests_empty"),
            message_id=message_id,
            markup=inline_keyboard([[menu_button("В меню", "main_menu")]]),
        )
        return

    items = [
        (
            f"#{row.id} {_request_status_label(row.status)} · {_request_snippet(row.source_text)}",
            _cb("req_detail", arg=row.id, page=page),
        )
        for row in requests
    ]
    rows = paginated_keyboard(
        items,
        page=page,
        page_size=_PAGE_SIZE,
        nav_callback=_cb("requests", page=0),
    )
    rows.append([menu_button("В меню", "main_menu")])
    await _send_or_edit(
        telegram,
        chat_id=chat_id,
        text=render_template("admin_requests_list"),
        message_id=message_id,
        markup=inline_keyboard(rows),
    )


async def _send_request_detail(
    session: AsyncSession,
    telegram: TelegramClientProtocol,
    chat_id: int,
    request_id: int,
    *,
    list_page: int = 0,
    message_id: int | None = None,
) -> None:
    request = await admin_service.get_request(session, request_id)
    if request is None:
        await _send_request_list(
            session, telegram, chat_id, list_page, message_id=message_id
        )
        return

    rows: list[list[dict[str, Any]]] = []
    if _request_is_open(request.status):
        rows.append(
            [menu_button("Завершить", "req_cancel", request.id, list_page)]
        )
    rows.append(
        [menu_button("Удалить", "req_purge_ask", request.id, list_page)]
    )
    rows.append(
        [
            menu_button("К заявкам", "requests", page=list_page),
            menu_button("В меню", "main_menu"),
        ]
    )
    await _send_or_edit(
        telegram,
        chat_id=chat_id,
        text=await render_admin_request_detail(
            session,
            request,
            status_label=_request_status_label(request.status),
        ),
        message_id=message_id,
        markup=inline_keyboard(rows),
    )


def _yn(value: bool) -> str:
    return "да" if value else "нет"


async def _send_business_status(
    session: AsyncSession,
    telegram: TelegramClientProtocol,
    chat_id: int,
    *,
    message_id: int | None = None,
) -> None:
    connections = await admin_service.list_business_connections(session)
    if connections:
        connections_block = "\n".join(
            (
                f"аккаунт {row.tg_user_id}: enabled={_yn(row.is_enabled)}, "
                f"can_reply={_yn(row.can_reply)}, "
                f"can_read={_yn(row.can_read_messages)}"
            )
            for row in connections
        )
    else:
        connections_block = "нет"

    pairs = await admin_service.list_business_dm_suppliers(session)
    if pairs:
        suppliers_block = "\n".join(
            (
                f"{supplier.name} (#{supplier.id}): chat_id={chat.chat_id} "
                f"active={_yn(chat.active)} default={_yn(chat.is_default)}"
            )
            for supplier, chat in pairs
        )
    else:
        suppliers_block = "нет"

    await _send_or_edit(
        telegram,
        chat_id=chat_id,
        text=render_template(
            "admin_business_status",
            connections_block=connections_block,
            suppliers_block=suppliers_block,
        ),
        message_id=message_id,
        markup=inline_keyboard([[menu_button("В меню", "main_menu")]]),
    )


async def _send_supplier_list(
    session: AsyncSession,
    telegram: TelegramClientProtocol,
    chat_id: int,
    page: int,
    *,
    message_id: int | None = None,
) -> None:
    suppliers = await admin_service.list_suppliers(session)
    items = [
        (
            f"{s.name} {'✅' if s.active else '⛔'} {'📤' if s.rfq_enabled else ''}",
            _cb("supplier_detail", arg=s.id),
        )
        for s in suppliers
    ]
    rows = paginated_keyboard(
        items,
        page=page,
        page_size=_PAGE_SIZE,
        nav_callback=_cb("suppliers", page=0),
    )
    rows.append(
        [menu_button("➕ Добавить поставщика", "supplier_add_start"), menu_button("В меню", "main_menu")]
    )
    await _send_or_edit(
        telegram,
        chat_id=chat_id,
        text=render_template("admin_supplier_list"),
        message_id=message_id,
        markup=inline_keyboard(rows),
    )


async def _send_employee_list(
    session: AsyncSession,
    telegram: TelegramClientProtocol,
    chat_id: int,
    page: int,
    *,
    message_id: int | None = None,
) -> None:
    employees = await admin_service.list_employees(session)
    items = [
        (
            f"{e.name or '—'} {'✅' if e.active else '⛔'} ({e.telegram_id})",
            _cb("employee_detail", arg=e.id),
        )
        for e in employees
    ]
    rows = paginated_keyboard(
        items,
        page=page,
        page_size=_PAGE_SIZE,
        nav_callback=_cb("employees", page=0),
    )
    rows.append(
        [
            menu_button("➕ Добавить сотрудника", "employee_add_start"),
            menu_button("В меню", "main_menu"),
        ]
    )
    await _send_or_edit(
        telegram,
        chat_id=chat_id,
        text=render_template("admin_employees_list"),
        message_id=message_id,
        markup=inline_keyboard(rows),
    )


async def _send_employee_detail(
    session: AsyncSession,
    telegram: TelegramClientProtocol,
    chat_id: int,
    employee_id: int,
    *,
    message_id: int | None = None,
) -> None:
    employee = await session.get(Employee, employee_id)
    if employee is None:
        await _send_employee_list(session, telegram, chat_id, 0, message_id=message_id)
        return

    text = render_template(
        "admin_employee_detail",
        employee_id=employee.id,
        employee_name=employee.name or "—",
        telegram_id=employee.telegram_id,
        active=employee.active,
    )
    rows = [
        [
            menu_button(
                "⛔ Вкл/выкл" if employee.active else "✅ Вкл/выкл",
                "employee_toggle_active",
                employee_id,
            ),
        ],
        [menu_button("Удалить", "emp_del_ask", employee_id)],
        [menu_button("К списку", "employees", page=0), menu_button("В меню", "main_menu")],
    ]
    await _send_or_edit(
        telegram,
        chat_id=chat_id,
        text=text,
        message_id=message_id,
        markup=inline_keyboard(rows),
    )


def _supplier_price_channel_label(supplier: Supplier) -> str:
    if supplier.price_channel_id is None:
        return "не привязан"
    return supplier.price_channel_username or str(supplier.price_channel_id)


def _categories_label(enabled: set[str]) -> str:
    labels = [
        _CATEGORY_LABELS[category]
        for category in _CATEGORY_ORDER
        if category.value in enabled
    ]
    return ", ".join(labels) if labels else "—"


async def _send_supplier_detail(
    session: AsyncSession,
    telegram: TelegramClientProtocol,
    chat_id: int,
    supplier_id: int,
    *,
    message_id: int | None = None,
) -> None:
    supplier = await session.get(Supplier, supplier_id)
    if supplier is None:
        await _send_supplier_list(session, telegram, chat_id, 0, message_id=message_id)
        return

    enabled = await admin_service.list_supplier_categories(session, supplier.id)
    text = render_template(
        "admin_supplier_detail",
        supplier_id=supplier.id,
        supplier_name=supplier.name,
        active=supplier.active,
        rfq_enabled=supplier.rfq_enabled,
        telegram_id=supplier.telegram_id or "—",
        dm_ok=supplier.dm_ok,
        price_channel_label=_supplier_price_channel_label(supplier),
        categories_label=_categories_label(enabled),
    )
    rows = [
        [
            menu_button("✏️ Переименовать", "supplier_rename_start", supplier_id),
            menu_button("⛔ Вкл/выкл" if supplier.active else "✅ Вкл/выкл", "supplier_toggle_active", supplier_id),
        ],
        [
            menu_button("📤 RFQ вкл" if supplier.rfq_enabled else "📤 RFQ выкл", "supplier_toggle_rfq", supplier_id),
        ],
        [
            menu_button("📂 Категории", "sup_cat", supplier_id),
        ],
        [
            menu_button("📡 Канал прайса", "supplier_channel", supplier_id),
        ],
        [
            menu_button(
                "📱 Привязать личку" if supplier.telegram_id is None else "🔄 Сменить личку",
                "supplier_bind_start",
                supplier_id,
            ),
            menu_button("🗑 Отвязать личку", "supplier_unbind", supplier_id)
            if supplier.telegram_id is not None
            else button("", cd=_cb("noop")),
        ],
        [
            menu_button("Чаты поставщика", "supplier_chats", supplier_id),
        ],
        [menu_button("Удалить", "sup_del_ask", supplier_id)],
        [menu_button("К списку", "suppliers", page=0), menu_button("В меню", "main_menu")],
    ]
    await _send_or_edit(
        telegram,
        chat_id=chat_id,
        text=text,
        message_id=message_id,
        markup=inline_keyboard(rows),
    )


async def _send_supplier_categories(
    session: AsyncSession,
    telegram: TelegramClientProtocol,
    chat_id: int,
    supplier_id: int,
    *,
    message_id: int | None = None,
) -> None:
    supplier = await session.get(Supplier, supplier_id)
    if supplier is None:
        await _send_supplier_list(session, telegram, chat_id, 0, message_id=message_id)
        return
    enabled = await admin_service.list_supplier_categories(session, supplier_id)
    rows: list[list[dict[str, Any]]] = []
    for index, category in enumerate(_CATEGORY_ORDER):
        mark = "✅" if category.value in enabled else "❌"
        rows.append(
            [
                menu_button(
                    f"{mark} {_CATEGORY_LABELS[category]}",
                    "sup_cat_tog",
                    supplier_id,
                    page=index,
                )
            ]
        )
    rows.append([menu_button("К поставщику", "supplier_detail", supplier_id)])
    await _send_or_edit(
        telegram,
        chat_id=chat_id,
        text=render_template(
            "admin_supplier_categories",
            supplier_id=supplier.id,
            supplier_name=supplier.name,
        ),
        message_id=message_id,
        markup=inline_keyboard(rows),
    )


async def _send_supplier_channel_list(
    session: AsyncSession,
    telegram: TelegramClientProtocol,
    chat_id: int,
    supplier_id: int,
    *,
    message_id: int | None = None,
) -> None:
    try:
        channels = await parser_client.list_channels()
    except parser_client.ParserClientError as exc:
        await _send_or_edit(
            telegram,
            chat_id=chat_id,
            text=render_template("admin_price_channels_error", detail=str(exc)),
            message_id=message_id,
            markup=inline_keyboard(
                [[menu_button("К поставщику", "supplier_detail", supplier_id)]]
            ),
        )
        return

    price_channels = [
        ch for ch in channels if _is_listed_price_channel(ch) and ch.is_active
    ]
    rows: list[list[dict[str, Any]]] = [
        [menu_button("Не привязан / отвязать", "supplier_unbind_ch", supplier_id)]
    ]
    for ch in price_channels:
        handle = ch.username or ch.title or f"#{ch.id}"
        if ch.channel_id is not None and ch.is_active and ch.status == "active":
            label = f"✅ {handle}"
        else:
            label = f"⏳ {handle} (готовится)"
        rows.append(
            [menu_button(label, "supplier_bind_ch", supplier_id, ch.id)]
        )
    rows.append(
        [
            menu_button("К поставщику", "supplier_detail", supplier_id),
            menu_button("В меню", "main_menu"),
        ]
    )
    await _send_or_edit(
        telegram,
        chat_id=chat_id,
        text=render_template("admin_supplier_channel_pick", supplier_id=supplier_id),
        message_id=message_id,
        markup=inline_keyboard(rows),
    )


async def _send_supplier_chats(
    session: AsyncSession,
    telegram: TelegramClientProtocol,
    chat_id: int,
    supplier_id: int,
    *,
    message_id: int | None = None,
) -> None:
    chats = await admin_service.list_supplier_chats(session, supplier_id)
    text = render_template(
        "admin_supplier_chats",
        supplier_id=supplier_id,
        count=len(chats),
    )
    rows: list[list[dict[str, Any]]] = []
    for c in chats:
        star = "⭐" if c.is_default else "  "
        active = "✅" if c.active else "⛔"
        label = f"{star} {active} {c.title or c.chat_id} ({c.chat_type.value})"
        rows.append(
            [
                button(label, cd=_cb("noop", arg=c.chat_id)),
                menu_button("⭐" if not c.is_default else "—", "supplier_set_default", supplier_id, c.chat_id),
                menu_button("Вкл" if not c.active else "Выкл", "supplier_toggle_chat", supplier_id, c.chat_id),
            ]
        )
    rows.append(
        [menu_button("К поставщику", "supplier_detail", supplier_id), menu_button("В меню", "main_menu")]
    )
    await _send_or_edit(
        telegram,
        chat_id=chat_id,
        text=text,
        message_id=message_id,
        markup=inline_keyboard(rows),
    )


async def _send_client_groups(
    session: AsyncSession,
    telegram: TelegramClientProtocol,
    chat_id: int,
    *,
    message_id: int | None = None,
) -> None:
    groups = await admin_service.list_client_groups(session)
    rows: list[list[dict[str, Any]]] = []
    for g in groups:
        label = f"{'✅' if g.active else '⛔'} {g.title or g.chat_id}"
        rows.append(
            [
                button(label, cd=_cb("noop", arg=g.id)),
                menu_button("Вкл" if not g.active else "Выкл", "client_group_toggle", g.id),
            ]
        )
    rows.append([menu_button("В меню", "main_menu")])
    await _send_or_edit(
        telegram,
        chat_id=chat_id,
        text=render_template("admin_client_groups"),
        message_id=message_id,
        markup=inline_keyboard(rows),
    )


async def _send_pending_chats(
    session: AsyncSession,
    telegram: TelegramClientProtocol,
    chat_id: int,
    *,
    message_id: int | None = None,
) -> None:
    result = await session.execute(
        select(PendingChat).order_by(PendingChat.created_at.desc())
    )
    pending = list(result.scalars().all())
    rows: list[list[dict[str, Any]]] = []
    for p in pending:
        label = f"{p.title or p.chat_id} ({p.chat_type.value})"
        rows.append(
            [
                button(label, cd=_cb("noop", arg=p.chat_id)),
                menu_button("Поставщик", "pending_as_supplier", p.chat_id),
                menu_button("Клиенты", "pending_as_client", p.chat_id),
                menu_button("Игнор", "pending_ignore", p.chat_id),
            ]
        )
    rows.append([menu_button("В меню", "main_menu")])
    await _send_or_edit(
        telegram,
        chat_id=chat_id,
        text=render_template("admin_pending_chats"),
        message_id=message_id,
        markup=inline_keyboard(rows),
    )


async def _send_bind_supplier_list(
    session: AsyncSession,
    telegram: TelegramClientProtocol,
    chat_id: int,
    pending_chat_id: int,
    *,
    page: int,
    message_id: int | None = None,
) -> None:
    suppliers = await admin_service.list_suppliers(session)
    items = [
        (s.name, _cb("bind_supplier_to_chat", arg=pending_chat_id, page=s.id))
        for s in suppliers
    ]
    rows = paginated_keyboard(
        items,
        page=page,
        page_size=_PAGE_SIZE,
        nav_callback=_cb("pending_as_supplier", arg=pending_chat_id, page=0),
    )
    rows.append([menu_button("Назад", "pending_chats")])
    await _send_or_edit(
        telegram,
        chat_id=chat_id,
        text=render_template("admin_bind_supplier"),
        message_id=message_id,
        markup=inline_keyboard(rows),
    )


def _is_listed_price_channel(ch: parser_client.ParserChannel) -> bool:
    """Admin «Каналы прайсов» shows live rows plus soft-deleted/failed ones for restore."""
    return bool(ch.is_active) or ch.status == "failed"


def _price_channel_label(ch: parser_client.ParserChannel) -> str:
    name = ch.title or ch.username or ch.channel_id or ch.id
    if not ch.is_active:
        return f"🗑 {name} (удалён)"
    if ch.status == "active" and ch.channel_id is not None:
        icon = "✅"
    elif ch.status == "failed":
        icon = "❌"
    else:
        icon = "⏳"
    reason = (ch.error_text or "").strip()
    if ch.status == "failed" and reason:
        return f"{icon} {name} ({reason[:40]})"
    return f"{icon} {name}"


async def _send_price_channels(
    session: AsyncSession,
    telegram: TelegramClientProtocol,
    chat_id: int,
    *,
    message_id: int | None = None,
    extra_channel: parser_client.ParserChannel | None = None,
    notice: str | None = None,
) -> None:
    try:
        channels = await parser_client.list_channels()
    except parser_client.ParserClientError as exc:
        text = render_template("admin_price_channels_error", detail=str(exc))
        await _send_or_edit(
            telegram,
            chat_id=chat_id,
            text=text,
            message_id=message_id,
            markup=inline_keyboard([[menu_button("В меню", "main_menu")]]),
        )
        return

    if extra_channel is not None and all(ch.id != extra_channel.id for ch in channels):
        channels = [*channels, extra_channel]

    visible = [ch for ch in channels if _is_listed_price_channel(ch)]
    lines = [_price_channel_label(ch) for ch in visible]
    channels_block = "\n".join(lines) if lines else "Список пуст."

    rows: list[list[dict[str, Any]]] = []
    for ch in visible:
        label = _price_channel_label(ch)
        action_button = (
            menu_button("Удалить", "price_channel_delete", ch.id)
            if ch.is_active
            else menu_button("Восстановить", "price_channel_restore", ch.id)
        )
        rows.append(
            [
                button(label, cd=_cb("noop", arg=ch.id)),
                action_button,
            ]
        )
    rows.append(
        [menu_button("➕ Добавить канал", "price_channel_add_start"), menu_button("В меню", "main_menu")]
    )
    text = render_template("admin_price_channels", channels_block=channels_block)
    if notice:
        text = f"{notice}\n\n{text}"
    await _send_or_edit(
        telegram,
        chat_id=chat_id,
        text=text,
        message_id=message_id,
        markup=inline_keyboard(rows),
    )


_PARSER_TAB_LABELS = ("Цены", "Каналы и статус")
_MANUAL_MORNING_PRICE_TIMEOUT = 300.0
_PARSER_LINE_LIMIT = 120
_PARSER_READY_REASON_LIMIT = 200
_PARSER_READY_REASON_COUNT = 3
_manual_morning_price_tasks: set[asyncio.Task[None]] = set()


def _clip(text: str, limit: int) -> str:
    """Обрезать строку экрана, чтобы сообщение уложилось в лимит Telegram."""
    cleaned = text.strip()
    if len(cleaned) <= limit:
        return cleaned
    if limit <= 1:
        return cleaned[:limit]
    return cleaned[: limit - 1] + "…"


def _ready_reasons_text(reasons: object) -> str:
    """Не больше трёх причин готовности парсера и 200 символов."""
    if not isinstance(reasons, list):
        return "—"
    parts = [str(item).strip() for item in reasons if str(item).strip()]
    if not parts:
        return "—"
    return _clip(", ".join(parts[:_PARSER_READY_REASON_COUNT]), _PARSER_READY_REASON_LIMIT)


def _parser_is_today(value: datetime | None) -> bool:
    if value is None:
        return False
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    tz = ZoneInfo(get_settings().tz)
    return value.astimezone(tz).date() == datetime.now(tz).date()


def _parser_short_dt(value: datetime | None) -> str:
    if value is None:
        return "—"
    return value.astimezone(ZoneInfo(get_settings().tz)).strftime("%d.%m %H:%M")


async def _parser_prices_block(session: AsyncSession) -> tuple[list[str], list[str]]:
    """Цены, которые парсер достал из каналов за сегодня, + состояние черновика."""
    from decimal import Decimal

    from app.services import parser_diagnostics, price_service

    aggregated = await price_service.aggregate_today(session)
    lines = [
        "{} — {} {}".format(
            _clip(
                price_service.format_sku_title(
                    model=row.get("model"),
                    storage=row.get("storage"),
                    color=row.get("color"),
                    region=row.get("region"),
                    sim=row.get("sim"),
                ),
                _PARSER_LINE_LIMIT,
            ),
            Decimal(str(row["min_price"])).quantize(Decimal("0.01")),
            row.get("currency") or "RUB",
        )
        for row in aggregated
    ]

    draft = (
        await session.execute(
            select(PriceListDraft).order_by(PriceListDraft.id.desc()).limit(1)
        )
    ).scalar_one_or_none()
    meta: list[str] = []
    if draft is None:
        meta.append("Черновика прайса ещё не было.")
    elif _parser_is_today(draft.generated_at):
        items_count = len(draft.items) if isinstance(draft.items, list) else 0
        meta.append(
            f"Черновик #{draft.id}: статус {draft.status}, позиций {items_count}, "
            f"создан {_parser_short_dt(draft.generated_at)}"
        )
    else:
        meta.append("Сегодня черновика нет.")
    alert_lines = parser_diagnostics.format_last_alert_lines(
        await parser_diagnostics.last_alert_codes(session)
    )
    if alert_lines:
        meta.append(f"Почему прайс пуст:\n{alert_lines}")
    return lines, meta


async def _parser_channels_block(session: AsyncSession) -> tuple[list[str], list[str]]:
    """Каналы парсера, их статусы и привязка к активным поставщикам."""
    channels = await parser_client.list_channels()
    ready, ready_body = await parser_client.fetch_parser_ready()
    result = await session.execute(
        select(Supplier).where(Supplier.price_channel_id.is_not(None))
    )
    bound = {
        int(supplier.price_channel_id): supplier
        for supplier in result.scalars().all()
        if supplier.price_channel_id is not None
    }

    lines: list[str] = []
    bound_count = 0
    for ch in channels:
        if not _is_listed_price_channel(ch):
            continue
        supplier = bound.get(int(ch.channel_id)) if ch.channel_id is not None else None
        if supplier is not None:
            bound_count += 1
        sync_at = supplier.last_price_sync_at if supplier is not None else None
        target = supplier.name if supplier is not None else "не привязан"
        lines.append(
            f"{_price_channel_label(ch)} · id={ch.channel_id or '—'} · {target} · "
            f"синхр. {_parser_short_dt(sync_at)}"
        )

    ready_line = "готов" if ready else "НЕ готов"
    meta = [f"Парсер {ready_line} · каналов {len(lines)}, привязано {bound_count}."]
    if not ready:
        meta.append(f"Причины: {_ready_reasons_text(ready_body.get('reasons'))}.")
    failed = [ch for ch in channels if ch.status == "failed" and ch.error_text]
    for ch in failed[:3]:
        meta.append(f"Ошибка {ch.title or ch.username or ch.id}: {(ch.error_text or '')[:60]}")
    return lines, meta


def _parser_pager(tab: int, page: int, total: int) -> list[list[dict[str, Any]]]:
    nav: list[dict[str, Any]] = []
    if page > 0:
        nav.append(menu_button("◀ Назад", "parser", tab, page=page - 1))
    if (page + 1) * _PAGE_SIZE < total:
        nav.append(menu_button("Вперед ▶", "parser", tab, page=page + 1))
    return [nav] if nav else []


async def _send_parser_screen(
    session: AsyncSession,
    telegram: TelegramClientProtocol,
    chat_id: int,
    *,
    tab: int,
    page: int = 0,
    message_id: int | None = None,
    notice: str | None = None,
) -> None:
    tab = tab if tab in (0, 1) else 0
    page = max(page, 0)
    try:
        if tab == 0:
            lines, meta = await _parser_prices_block(session)
        else:
            lines, meta = await _parser_channels_block(session)
    except parser_client.ParserClientError as exc:
        await _send_or_edit(
            telegram,
            chat_id=chat_id,
            text=render_template(
                "admin_parser_error",
                detail=f"{parser_client.classify_parser_error(exc)}: {exc}",
            ),
            message_id=message_id,
            markup=inline_keyboard(
                [
                    [menu_button("🔄 Повторить", "parser", tab)],
                    [menu_button("В меню", "main_menu")],
                ]
            ),
        )
        return

    total = len(lines)
    visible = lines[page * _PAGE_SIZE : (page + 1) * _PAGE_SIZE]
    pages = max(1, -(-total // _PAGE_SIZE))
    text = render_template(
        "admin_parser_screen",
        tab_title=_PARSER_TAB_LABELS[tab],
        body_block="\n".join(visible) if visible else "Пусто.",
        meta_block="\n".join([f"Стр. {page + 1} из {pages}", *meta]),
    )
    if notice:
        text = f"{notice}\n\n{text}"

    rows = _parser_pager(tab, page, total)
    rows.append(
        [
            menu_button(_PARSER_TAB_LABELS[1 - tab], "parser", 1 - tab),
            menu_button("🔄 Обновить", "parser", tab),
        ]
    )
    rows.append([menu_button("▶ Запустить прайс", "parser_run", tab)])
    rows.append([menu_button("В меню", "main_menu")])
    await _send_or_edit(
        telegram,
        chat_id=chat_id,
        text=text,
        message_id=message_id,
        markup=inline_keyboard(rows),
    )


def _morning_price_result_text(result: dict[str, Any]) -> str:
    if result.get("status") == "skipped":
        return "Утренний прайс уже выполняется — подожди минуту."
    draft_id = result.get("draft_id")
    notice = (
        f"Готово: позиций {result.get('items', 0)}, "
        f"новых прайсов {result.get('raw_prices_new', 0)}, "
        f"черновик {'#' + str(draft_id) if draft_id else 'не создан'}."
    )
    reasons = result.get("reason") or []
    if reasons:
        notice = f"{notice} Причины: {', '.join(str(code) for code in reasons)}."
    pull = result.get("parser_pull")
    if pull:
        notice = f"{pull} {notice}"
    return notice


async def _pull_bound_channel_posts(session: AsyncSession) -> str | None:
    """Ручной забор истории привязанных каналов. MTProto остаётся в парсере."""
    result = await session.execute(
        select(Supplier).where(
            Supplier.active.is_(True),
            Supplier.price_channel_id.is_not(None),
        )
    )
    suppliers = [
        supplier
        for supplier in result.scalars().all()
        if supplier.price_channel_id is not None
    ]
    if not suppliers:
        return None
    notes: list[str] = []
    pulled = 0
    for supplier in suppliers:
        channel_id = int(supplier.price_channel_id)
        try:
            status = await parser_client.sync_channel_history(channel_id)
        except parser_client.ParserClientError as exc:
            notes.append(
                f"{supplier.name}: парсер не принял забор "
                f"({parser_client.classify_parser_error(exc)})"
            )
            continue
        if status == "processed":
            pulled += 1
        else:
            notes.append(f"{supplier.name}: забор постов не завершён ({status})")
    if pulled and not notes:
        return "Посты канала забраны."
    if pulled:
        return f"Посты части каналов забраны. {' '.join(notes)}"
    if notes:
        return " ".join(notes)
    return None


async def _finish_manual_morning_price(chat_id: int) -> None:
    """Прогон утреннего прайса вне сессии апдейта: свой коннект, тот же лок, потолок 300 с."""
    from app import jobs
    from app.db.session import get_scheduler_session_factory
    from app.telegram.jobs_router import _run_locked

    telegram = get_telegram_client()
    text = "Утренний прайс не запустился. Подробности в логах."
    try:
        factory = get_scheduler_session_factory()
        async with factory() as session:
            try:
                async def _pull_then_price(locked_session, telegram_client):  # noqa: ANN001
                    note = await _pull_bound_channel_posts(locked_session)
                    price = await jobs.run_morning_price(locked_session, telegram_client)
                    if note and isinstance(price, dict):
                        return {**price, "parser_pull": note}
                    return price

                result = await asyncio.wait_for(
                    _run_locked(session, "morning-price", _pull_then_price),
                    timeout=_MANUAL_MORNING_PRICE_TIMEOUT,
                )
            except TimeoutError:
                logger.warning("Manual morning price timed out chat_id={}", chat_id)
                text = "Утренний прайс не уложился в 5 минут. Проверь парсер и логи."
            except Exception:
                logger.exception("Manual morning price failed chat_id={}", chat_id)
                try:
                    await session.commit()
                except Exception:
                    logger.exception("Manual morning price audit commit failed")
            else:
                try:
                    await session.commit()
                except Exception:
                    logger.exception("Manual morning price audit commit failed")
                text = _morning_price_result_text(result)
    except Exception:
        logger.exception("Manual morning price session failed chat_id={}", chat_id)
    try:
        await telegram.send_message(chat_id, text)
    except Exception:
        logger.exception("Failed to report manual morning price chat_id={}", chat_id)


def _schedule_manual_morning_price(chat_id: int) -> None:
    task = asyncio.create_task(
        _finish_manual_morning_price(chat_id),
        name="manual-morning-price",
    )
    _manual_morning_price_tasks.add(task)
    task.add_done_callback(_manual_morning_price_tasks.discard)


async def _run_parser_morning_price(
    session: AsyncSession,
    telegram: TelegramClientProtocol,
    chat_id: int,
    tab: int,
    message_id: int | None,
) -> None:
    """Сразу ответить на кнопку. Джоб идёт отдельной задачей и не держит апдейт."""
    await _send_parser_screen(
        session,
        telegram,
        chat_id,
        tab=tab if tab in (0, 1) else 0,
        message_id=message_id,
        notice="Запущено. Сначала забираю посты канала, потом собираю прайс.",
    )
    _schedule_manual_morning_price(chat_id)


async def _handle_dialog_text(
    session: AsyncSession,
    dialog: Any,
    owner_id: int,
    chat_id: int,
    message: dict[str, Any],
    telegram: TelegramClientProtocol,
) -> str:
    state = dialog.state
    payload = dialog.payload or {}
    text = extract_message_text(message)

    if state == "await_markup_amount":
        from decimal import Decimal, InvalidOperation

        from app.handlers.owner_commands import apply_markup_choice

        raw = text.replace(" ", "").replace(",", ".")
        try:
            amount = Decimal(raw)
        except InvalidOperation:
            await telegram.send_message(chat_id, "Сумма должна быть числом")
            return "ok"
        if amount < 0:
            await telegram.send_message(chat_id, "Сумма не может быть отрицательной")
            return "ok"
        rule_id = int(payload.get("rule_id") or 0)
        await admin_service.clear_dialog(session, owner_id)
        await apply_markup_choice(
            session,
            chat_id=chat_id,
            rule_id=rule_id,
            amount=amount,
            telegram=telegram,
        )
        return "ok"

    if state == "await_markup_rule_phrase":
        phrase = text.strip()
        if len(phrase) < 2:
            await telegram.send_message(
                chat_id, render_template("markup_add_enter_phrase")
            )
            return "ok"
        await admin_service.set_dialog(
            session,
            telegram_id=owner_id,
            state="await_markup_rule_amount",
            payload={"phrase": phrase[:200]},
        )
        await telegram.send_message(
            chat_id,
            render_template("markup_add_enter_amount", rule_label=phrase[:80]),
        )
        return "ok"

    if state == "await_markup_rule_amount":
        from decimal import Decimal, InvalidOperation

        from app.services.markup_service import create_markup_rule

        phrase = str(payload.get("phrase") or "").strip()
        if not phrase:
            await admin_service.clear_dialog(session, owner_id)
            await telegram.send_message(chat_id, render_template("admin_error", detail="нет названия товара"))
            return "ok"
        raw = text.replace(" ", "").replace(",", ".")
        try:
            amount = Decimal(raw)
        except InvalidOperation:
            await telegram.send_message(chat_id, "Сумма должна быть числом")
            return "ok"
        if amount < 0:
            await telegram.send_message(chat_id, "Сумма не может быть отрицательной")
            return "ok"
        try:
            rule = await create_markup_rule(
                session, phrase=phrase, markup_rub=amount
            )
        except ValueError:
            await telegram.send_message(
                chat_id, render_template("markup_add_enter_phrase")
            )
            return "ok"
        await admin_service.clear_dialog(session, owner_id)
        label = (rule.rule_key or phrase).replace("_", " ")
        await telegram.send_message(
            chat_id,
            render_template(
                "markup_rule_created",
                rule_label=label,
                amount=int(amount),
            ),
            reply_markup=inline_keyboard(
                [
                    [
                        menu_button("К наценкам", "markup_list"),
                        menu_button("В меню", "main_menu"),
                    ]
                ]
            ),
        )
        return "ok"

    if state == "await_supplier_name":
        name = text.strip()
        if not name:
            await telegram.send_message(chat_id, render_template("admin_need_name"))
            return "ok"
        supplier = await admin_service.add_supplier(
            session, name=name, bound_by_owner_id=owner_id
        )
        await admin_service.set_dialog(
            session,
            telegram_id=owner_id,
            state="await_supplier_bind",
            payload={"supplier_id": supplier.id},
        )
        await _send_or_edit(
            telegram,
            chat_id=chat_id,
            text=render_template("admin_supplier_bind_prompt"),
            message_id=message.get("message_id"),
            markup=inline_keyboard(
                [
                    [
                        menu_button("↩️ Переслать сообщение", "supplier_bind_forward", supplier.id),
                        menu_button("🔗 Ссылка", "supplier_bind_link", supplier.id),
                    ],
                    [menu_button("⏭ Пропустить", "supplier_bind_skip", supplier.id)],
                    [menu_button("Отмена", "main_menu")],
                ]
            ),
        )
        return "ok"

    if state == "await_channel_handle":
        handle = text.strip()
        if not handle:
            await telegram.send_message(chat_id, render_template("admin_need_channel"))
            return "ok"
        created = None
        try:
            created = await parser_client.add_channel(handle)
        except parser_client.ParserClientError as exc:
            detail = str(exc)
            if "parser_http_409" in detail:
                text_out = render_template("admin_channel_add_duplicate")
            elif "parser_http_422" in detail:
                text_out = render_template(
                    "admin_channel_add_error",
                    detail="нужен @username или числовой ID канала",
                )
            elif "parser_transport_error" in detail or "parser_api_url_missing" in detail:
                text_out = render_template("admin_channel_add_unavailable")
            else:
                text_out = render_template("admin_channel_add_error", detail=detail)
            await telegram.send_message(
                chat_id,
                text_out,
                reply_markup=inline_keyboard([[menu_button("К каналам", "price_channels")]]),
            )
            return "ok"
        finally:
            await admin_service.clear_dialog(session, owner_id)
        label = str(created.title or created.username or created.channel_id or handle)
        status_label = {
            "active": "подключён",
            "failed": "ошибка подключения",
            "pending": "подключается",
        }.get(created.status, created.status)
        await telegram.send_message(
            chat_id,
            render_template(
                "admin_channel_added",
                label=label,
                status_label=status_label,
            ),
        )
        await _send_price_channels(
            session, telegram, chat_id, extra_channel=created
        )
        return "ok"

    if state == "await_rename":
        supplier_id = payload.get("supplier_id")
        name = text.strip()
        if not name or supplier_id is None:
            await admin_service.clear_dialog(session, owner_id)
            await _send_main_menu(session, telegram, chat_id)
            return "ok"
        await admin_service.rename_supplier(session, supplier_id, name)
        await admin_service.clear_dialog(session, owner_id)
        await _send_supplier_detail(session, telegram, chat_id, supplier_id)
        return "ok"

    if state == "await_employee_name":
        name = text.strip()
        if not name:
            await telegram.send_message(
                chat_id, render_template("admin_employee_need_name")
            )
            return "ok"
        await admin_service.set_dialog(
            session,
            telegram_id=owner_id,
            state="await_employee_telegram_id",
            payload={"name": name},
        )
        await telegram.send_message(
            chat_id,
            render_template("admin_await_employee_telegram_id"),
            reply_markup=inline_keyboard(
                [[menu_button("Отмена", "employees", page=0)]]
            ),
        )
        return "ok"

    if state == "await_employee_telegram_id":
        name = str(payload.get("name") or "")
        if not name:
            await admin_service.clear_dialog(session, owner_id)
            await _send_main_menu(session, telegram, chat_id)
            return "ok"

        telegram_id = resolve_user_id_from_message(text, message)

        if telegram_id is None:
            if message.get("forward_from") or message.get("forward_origin"):
                await telegram.send_message(
                    chat_id, render_template("admin_employee_forward_hidden")
                )
            else:
                await telegram.send_message(
                    chat_id, render_template("admin_employee_invalid_telegram_id")
                )
            return "ok"

        try:
            employee = await admin_service.add_employee(
                session, name=name, telegram_id=telegram_id
            )
        except admin_service.DuplicateEmployeeTelegramIdError:
            await telegram.send_message(
                chat_id,
                render_template(
                    "admin_employee_duplicate_telegram_id",
                    telegram_id=telegram_id,
                ),
            )
            return "ok"

        await admin_service.clear_dialog(session, owner_id)
        await _send_employee_detail(session, telegram, chat_id, employee.id)
        return "ok"

    if state == "await_supplier_bind":
        supplier_id = payload.get("supplier_id")
        if supplier_id is None:
            await admin_service.clear_dialog(session, owner_id)
            await _send_main_menu(session, telegram, chat_id)
            return "ok"
        # This state is normally driven by callbacks; plain text here is ignored
        # but the dialog stays alive so owner can click the inline buttons.
        return "ok"

    if state == "await_supplier_forward":
        supplier_id = payload.get("supplier_id")
        if supplier_id is None:
            await admin_service.clear_dialog(session, owner_id)
            await _send_main_menu(session, telegram, chat_id)
            return "ok"

        telegram_id = resolve_user_id_from_message(text, message)

        if telegram_id is None:
            if message.get("forward_from") or message.get("forward_origin"):
                await telegram.send_message(
                    chat_id,
                    render_template(
                        "admin_supplier_forward_hidden",
                        supplier_id=supplier_id,
                    ),
                    reply_markup=inline_keyboard(
                        [
                            [menu_button("🔗 Сделать ссылку", "supplier_bind_link", supplier_id)],
                            [menu_button("Отмена", "supplier_detail", supplier_id)],
                        ]
                    ),
                )
            else:
                await telegram.send_message(
                    chat_id,
                    render_template("admin_supplier_invalid_telegram_id"),
                    reply_markup=inline_keyboard(
                        [[menu_button("Отмена", "supplier_detail", supplier_id)]]
                    ),
                )
            return "ok"

        try:
            supplier = await admin_service.bind_supplier_telegram_id(
                session,
                supplier_id=int(supplier_id),
                telegram_id=telegram_id,
                bound_by_owner_id=owner_id,
            )
        except admin_service.TelegramIdConflictError:
            await telegram.send_message(
                chat_id,
                render_template(
                    "admin_supplier_telegram_id_busy",
                    telegram_id=telegram_id,
                ),
                reply_markup=inline_keyboard(
                    [
                        [menu_button("🔗 Сделать ссылку", "supplier_bind_link", supplier_id)],
                        [menu_button("Отмена", "supplier_detail", supplier_id)],
                    ]
                ),
            )
            return "ok"

        await admin_service.clear_dialog(session, owner_id)
        await telegram.send_message(
            chat_id,
            render_template(
                "admin_supplier_bound",
                supplier_id=supplier.id,
                supplier_name=supplier.name,
                telegram_id=telegram_id,
            ),
        )
        await _send_supplier_detail(session, telegram, chat_id, supplier.id)
        return "ok"

    # Legacy dialog state: redirect to the new self-service bind flow.
    if state == "await_supplier_telegram_id":
        supplier_id = payload.get("supplier_id")
        if supplier_id is None:
            await admin_service.clear_dialog(session, owner_id)
            await _send_main_menu(session, telegram, chat_id)
            return "ok"
        telegram_id = resolve_user_id_from_message(text, message)
        if telegram_id is not None:
            try:
                await admin_service.bind_supplier_telegram_id(
                    session,
                    supplier_id=int(supplier_id),
                    telegram_id=telegram_id,
                    bound_by_owner_id=owner_id,
                )
            except admin_service.TelegramIdConflictError:
                await telegram.send_message(
                    chat_id,
                    render_template(
                        "admin_supplier_telegram_id_busy",
                        telegram_id=telegram_id,
                    ),
                    reply_markup=inline_keyboard(
                        [
                            [menu_button("🔗 Сделать ссылку", "supplier_bind_link", supplier_id)],
                            [menu_button("Отмена", "supplier_detail", supplier_id)],
                        ]
                    ),
                )
                return "ok"
            await admin_service.clear_dialog(session, owner_id)
            await _send_supplier_detail(session, telegram, chat_id, int(supplier_id))
            return "ok"

        if message.get("forward_from") or message.get("forward_origin"):
            await telegram.send_message(
                chat_id,
                render_template(
                    "admin_supplier_forward_hidden",
                    supplier_id=supplier_id,
                ),
                reply_markup=inline_keyboard(
                    [
                        [menu_button("🔗 Сделать ссылку", "supplier_bind_link", supplier_id)],
                        [menu_button("Отмена", "supplier_detail", supplier_id)],
                    ]
                ),
            )
            return "ok"

        await telegram.send_message(
            chat_id,
            render_template("admin_supplier_invalid_telegram_id"),
            reply_markup=inline_keyboard(
                [[menu_button("Отмена", "supplier_detail", supplier_id)]]
            ),
        )
        return "ok"

    await admin_service.clear_dialog(session, owner_id)
    await telegram.send_message(chat_id, render_template("admin_dialog_expired"))
    return "ok"
