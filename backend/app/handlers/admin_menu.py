"""Owner/admin inline menu for managing suppliers, chats, groups, channels.

All DB mutations are delegated to admin_service.py; this module only handles
Telegram UI and callback data.
"""

from __future__ import annotations

from typing import Any

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Employee, PendingChat, Supplier
from app.services import admin_service, parser_client, purge_service
from app.telegram.client import TelegramClientProtocol, get_telegram_client
from app.telegram.keyboards import CallbackData, button, inline_keyboard, menu_button, paginated_keyboard
from app.templates.messages_ru import render_template
from app.utils.telegram import (
    extract_forwarded_user_id,
    extract_message_text,
    parse_telegram_id_text,
)
from app.utils.whitelist import get_owner_by_telegram_id

_PAGE_SIZE = 8


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

    if text.startswith("/menu") or text.startswith("/admin") or text.startswith("/"):
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
            text=render_template("admin_error", detail=str(exc)),
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

    if action == "supplier_chats":
        await _send_supplier_chats(
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
        await parser_client.delete_channel(int(cd.arg))
        await _send_price_channels(session, telegram, chat_id, message_id=message_id)
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


async def _send_main_menu(
    session: AsyncSession,
    telegram: TelegramClientProtocol,
    chat_id: int,
    *,
    message_id: int | None = None,
) -> None:
    count = await _pending_count(session)
    rows = [
        [menu_button("Поставщики", "suppliers", page=0)],
        [menu_button("Сотрудники", "employees", page=0)],
        [menu_button("Клиентские беседы", "client_groups")],
        [menu_button(f"Новые чаты ({count})", "pending_chats")],
        [menu_button("Каналы прайсов", "price_channels")],
    ]
    await _send_or_edit(
        telegram,
        chat_id=chat_id,
        text=render_template("admin_main_menu"),
        message_id=message_id,
        markup=inline_keyboard(rows),
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
        [menu_button("К списку", "employees", page=0), menu_button("В меню", "main_menu")],
    ]
    await _send_or_edit(
        telegram,
        chat_id=chat_id,
        text=text,
        message_id=message_id,
        markup=inline_keyboard(rows),
    )


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

    text = render_template(
        "admin_supplier_detail",
        supplier_id=supplier.id,
        supplier_name=supplier.name,
        active=supplier.active,
        rfq_enabled=supplier.rfq_enabled,
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
            menu_button("Чаты поставщика", "supplier_chats", supplier_id),
        ],
        [menu_button("К списку", "suppliers", page=0), menu_button("В меню", "main_menu")],
    ]
    await _send_or_edit(
        telegram,
        chat_id=chat_id,
        text=text,
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


async def _send_price_channels(
    session: AsyncSession,
    telegram: TelegramClientProtocol,
    chat_id: int,
    *,
    message_id: int | None = None,
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

    rows: list[list[dict[str, Any]]] = []
    for ch in channels:
        label = f"{'✅' if ch.is_active else '⏳'} {ch.title or ch.username or ch.channel_id}"
        rows.append(
            [
                button(label, cd=_cb("noop", arg=ch.id)),
                menu_button("Удалить", "price_channel_delete", ch.id),
            ]
        )
    rows.append(
        [menu_button("➕ Добавить канал", "price_channel_add_start"), menu_button("В меню", "main_menu")]
    )
    await _send_or_edit(
        telegram,
        chat_id=chat_id,
        text=render_template("admin_price_channels"),
        message_id=message_id,
        markup=inline_keyboard(rows),
    )


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

    if state == "await_supplier_name":
        name = text.strip()
        if not name:
            await telegram.send_message(chat_id, render_template("admin_need_name"))
            return "ok"
        supplier = await admin_service.add_supplier(
            session, name=name, bound_by_owner_id=owner_id
        )
        await admin_service.clear_dialog(session, owner_id)
        await _send_supplier_detail(session, telegram, chat_id, supplier.id)
        return "ok"

    if state == "await_channel_handle":
        handle = text.strip()
        if not handle:
            await telegram.send_message(chat_id, render_template("admin_need_channel"))
            return "ok"
        try:
            await parser_client.add_channel(handle)
        except parser_client.ParserClientError as exc:
            await telegram.send_message(
                chat_id,
                render_template("admin_channel_add_error", detail=str(exc)),
                reply_markup=inline_keyboard([[menu_button("К каналам", "price_channels")]]),
            )
            return "ok"
        finally:
            await admin_service.clear_dialog(session, owner_id)
        await _send_price_channels(session, telegram, chat_id)
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
        name = payload.get("name")
        if not name:
            await admin_service.clear_dialog(session, owner_id)
            await _send_main_menu(session, telegram, chat_id)
            return "ok"

        telegram_id = parse_telegram_id_text(text)
        if telegram_id is None:
            telegram_id = extract_forwarded_user_id(message)

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

    await admin_service.clear_dialog(session, owner_id)
    await telegram.send_message(chat_id, render_template("admin_dialog_expired"))
    return "ok"
