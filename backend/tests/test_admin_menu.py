"""Tests for owner admin menu via inline callbacks."""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    AdminDialog,
    Employee,
    Owner,
    Supplier,
    SupplierChat,
    SupplierChatType,
)
from app.services import parser_client
from app.telegram.keyboards import CallbackData
from app.utils.whitelist import is_employee

OWNER_TG_ID = 300300300


def _callback_payload(
    owner_id: int,
    callback_id: str,
    data: CallbackData,
    *,
    chat_id: int = OWNER_TG_ID,
    message_id: int = 1,
) -> dict:
    return {
        "update_id": 100,
        "callback_query": {
            "id": callback_id,
            "from": {"id": owner_id},
            "message": {
                "message_id": message_id,
                "chat": {"id": chat_id, "type": "private"},
            },
            "data": data.encode(),
        },
    }


def _menu_command_payload(owner_id: int, text: str = "/menu") -> dict:
    return {
        "update_id": 100,
        "message": {
            "message_id": 1,
            "from": {"id": owner_id},
            "chat": {"id": owner_id, "type": "private"},
            "text": text,
        },
    }


def _owner_text_payload(
    owner_id: int,
    *,
    update_id: int,
    text: str = "",
    forward_from: dict | None = None,
    forward_origin: dict | None = None,
) -> dict:
    message: dict = {
        "message_id": update_id * 10,
        "from": {"id": owner_id},
        "chat": {"id": owner_id, "type": "private"},
        "text": text,
    }
    if forward_from is not None:
        message["forward_from"] = forward_from
    if forward_origin is not None:
        message["forward_origin"] = forward_origin
    return {"update_id": update_id, "message": message}


def _callback_data_from_markup(markup: dict | None) -> list[str]:
    if markup is None:
        return []
    rows = markup.get("inline_keyboard") or []
    result: list[str] = []
    for row in rows:
        for btn in row:
            data = btn.get("callback_data")
            if data:
                result.append(data)
    return result


@pytest.mark.asyncio
async def test_main_menu_has_employees_button(
    webhook_client: AsyncClient,
    webhook_headers: dict[str, str],
    seed_owner: Owner,
    mock_telegram,
) -> None:
    resp = await webhook_client.post(
        "/telegram/webhook",
        json=_menu_command_payload(OWNER_TG_ID),
        headers=webhook_headers,
    )
    assert resp.json()["status"] == "ok"

    markup = mock_telegram.sent[-1][2] if mock_telegram.sent else mock_telegram.edited[-1][3]
    callbacks = _callback_data_from_markup(markup)
    assert any("employees" in data for data in callbacks)


@pytest.mark.asyncio
async def test_add_employee_dialog(
    db_session: AsyncSession,
    webhook_client: AsyncClient,
    webhook_headers: dict[str, str],
    seed_owner: Owner,
) -> None:
    resp = await webhook_client.post(
        "/telegram/webhook",
        json=_callback_payload(
            OWNER_TG_ID,
            "cb_emp_add",
            CallbackData(namespace="admin", action="employee_add_start"),
        ),
        headers=webhook_headers,
    )
    assert resp.json()["status"] == "ok"

    dialog = await db_session.get(AdminDialog, OWNER_TG_ID)
    assert dialog is not None
    assert dialog.state == "await_employee_name"

    resp = await webhook_client.post(
        "/telegram/webhook",
        json=_owner_text_payload(OWNER_TG_ID, update_id=101, text="Менеджер А"),
        headers=webhook_headers,
    )
    assert resp.json()["status"] == "ok"

    dialog = await db_session.get(AdminDialog, OWNER_TG_ID)
    assert dialog is not None
    assert dialog.state == "await_employee_telegram_id"
    assert dialog.payload == {"name": "Менеджер А"}

    resp = await webhook_client.post(
        "/telegram/webhook",
        json=_owner_text_payload(OWNER_TG_ID, update_id=102, text="777888999"),
        headers=webhook_headers,
    )
    assert resp.json()["status"] == "ok"

    employee = (
        await db_session.execute(
            select(Employee).where(Employee.telegram_id == 777888999)
        )
    ).scalar_one_or_none()
    assert employee is not None
    assert employee.name == "Менеджер А"
    assert employee.active is True

    dialog = await db_session.get(AdminDialog, OWNER_TG_ID)
    assert dialog is None


@pytest.mark.asyncio
async def test_add_employee_via_forward_from(
    db_session: AsyncSession,
    webhook_client: AsyncClient,
    webhook_headers: dict[str, str],
    seed_owner: Owner,
) -> None:
    await webhook_client.post(
        "/telegram/webhook",
        json=_callback_payload(
            OWNER_TG_ID,
            "cb_emp_add2",
            CallbackData(namespace="admin", action="employee_add_start"),
        ),
        headers=webhook_headers,
    )
    await webhook_client.post(
        "/telegram/webhook",
        json=_owner_text_payload(OWNER_TG_ID, update_id=201, text="Менеджер B"),
        headers=webhook_headers,
    )
    resp = await webhook_client.post(
        "/telegram/webhook",
        json=_owner_text_payload(
            OWNER_TG_ID,
            update_id=202,
            forward_from={"id": 888777666, "is_bot": False},
        ),
        headers=webhook_headers,
    )
    assert resp.json()["status"] == "ok"

    employee = (
        await db_session.execute(
            select(Employee).where(Employee.telegram_id == 888777666)
        )
    ).scalar_one_or_none()
    assert employee is not None
    assert employee.name == "Менеджер B"


@pytest.mark.asyncio
async def test_add_employee_via_forward_origin(
    db_session: AsyncSession,
    webhook_client: AsyncClient,
    webhook_headers: dict[str, str],
    seed_owner: Owner,
) -> None:
    await webhook_client.post(
        "/telegram/webhook",
        json=_callback_payload(
            OWNER_TG_ID,
            "cb_emp_add3",
            CallbackData(namespace="admin", action="employee_add_start"),
        ),
        headers=webhook_headers,
    )
    await webhook_client.post(
        "/telegram/webhook",
        json=_owner_text_payload(OWNER_TG_ID, update_id=301, text="Менеджер C"),
        headers=webhook_headers,
    )
    resp = await webhook_client.post(
        "/telegram/webhook",
        json=_owner_text_payload(
            OWNER_TG_ID,
            update_id=302,
            forward_origin={
                "type": "user",
                "sender_user": {"id": 666555444, "is_bot": False},
            },
        ),
        headers=webhook_headers,
    )
    assert resp.json()["status"] == "ok"

    employee = (
        await db_session.execute(
            select(Employee).where(Employee.telegram_id == 666555444)
        )
    ).scalar_one_or_none()
    assert employee is not None


@pytest.mark.asyncio
async def test_add_employee_invalid_telegram_id_keeps_dialog(
    db_session: AsyncSession,
    webhook_client: AsyncClient,
    webhook_headers: dict[str, str],
    seed_owner: Owner,
) -> None:
    await webhook_client.post(
        "/telegram/webhook",
        json=_callback_payload(
            OWNER_TG_ID,
            "cb_emp_add4",
            CallbackData(namespace="admin", action="employee_add_start"),
        ),
        headers=webhook_headers,
    )
    await webhook_client.post(
        "/telegram/webhook",
        json=_owner_text_payload(OWNER_TG_ID, update_id=401, text="Менеджер D"),
        headers=webhook_headers,
    )
    resp = await webhook_client.post(
        "/telegram/webhook",
        json=_owner_text_payload(OWNER_TG_ID, update_id=402, text="not-a-number"),
        headers=webhook_headers,
    )
    assert resp.json()["status"] == "ok"

    count = (
        await db_session.execute(select(Employee).where(Employee.name == "Менеджер D"))
    ).scalars().all()
    assert count == []

    dialog = await db_session.get(AdminDialog, OWNER_TG_ID)
    assert dialog is not None
    assert dialog.state == "await_employee_telegram_id"


@pytest.mark.asyncio
async def test_add_employee_duplicate_inactive_rejected(
    db_session: AsyncSession,
    webhook_client: AsyncClient,
    webhook_headers: dict[str, str],
    seed_owner: Owner,
) -> None:
    db_session.add(
        Employee(telegram_id=555444333, name="Old Manager", active=False)
    )
    await db_session.flush()

    await webhook_client.post(
        "/telegram/webhook",
        json=_callback_payload(
            OWNER_TG_ID,
            "cb_emp_add5",
            CallbackData(namespace="admin", action="employee_add_start"),
        ),
        headers=webhook_headers,
    )
    await webhook_client.post(
        "/telegram/webhook",
        json=_owner_text_payload(OWNER_TG_ID, update_id=501, text="New Manager"),
        headers=webhook_headers,
    )
    resp = await webhook_client.post(
        "/telegram/webhook",
        json=_owner_text_payload(OWNER_TG_ID, update_id=502, text="555444333"),
        headers=webhook_headers,
    )
    assert resp.json()["status"] == "ok"

    employees = (
        await db_session.execute(
            select(Employee).where(Employee.telegram_id == 555444333)
        )
    ).scalars().all()
    assert len(employees) == 1
    assert employees[0].active is False
    assert employees[0].name == "Old Manager"

    dialog = await db_session.get(AdminDialog, OWNER_TG_ID)
    assert dialog is not None
    assert dialog.state == "await_employee_telegram_id"


@pytest.mark.asyncio
async def test_toggle_employee_active(
    db_session: AsyncSession,
    webhook_client: AsyncClient,
    webhook_headers: dict[str, str],
    seed_owner: Owner,
) -> None:
    employee = Employee(telegram_id=111222333, name="Toggle Me", active=True)
    db_session.add(employee)
    await db_session.flush()

    payload = _callback_payload(
        OWNER_TG_ID,
        "cb_emp_toggle",
        CallbackData(namespace="admin", action="employee_toggle_active", arg=employee.id),
    )
    resp = await webhook_client.post(
        "/telegram/webhook", json=payload, headers=webhook_headers
    )
    assert resp.json()["status"] == "ok"

    await db_session.refresh(employee)
    assert employee.active is False
    assert await is_employee(db_session, employee.telegram_id) is False
    assert (
        await is_employee(db_session, employee.telegram_id, require_active=False) is True
    )


@pytest.mark.asyncio
async def test_deactivated_employee_ask_ignored(
    db_session: AsyncSession,
    webhook_client: AsyncClient,
    webhook_headers: dict[str, str],
    seed_group_chat_id: int,
    seed_supplier_count: int,
) -> None:
    employee = Employee(telegram_id=444333222, name="Inactive Ask", active=False)
    db_session.add(employee)
    await db_session.flush()

    payload = {
        "update_id": 60001,
        "message": {
            "message_id": 600010,
            "from": {"id": employee.telegram_id, "first_name": "Inactive"},
            "chat": {"id": seed_group_chat_id, "type": "supergroup"},
            "text": "/ask iPhone 17 256GB",
        },
    }
    resp = await webhook_client.post(
        "/telegram/webhook", json=payload, headers=webhook_headers
    )
    assert resp.json()["status"] == "ignored"


@pytest.mark.asyncio
async def test_menu_callback_requires_owner(
    webhook_client: AsyncClient,
    webhook_headers: dict[str, str],
) -> None:
    payload = _callback_payload(999, "cb1", CallbackData(namespace="admin", action="main_menu"))
    resp = await webhook_client.post("/telegram/webhook", json=payload, headers=webhook_headers)
    assert resp.json()["status"] == "ignored"


@pytest.mark.asyncio
async def test_add_supplier_dialog(
    db_session: AsyncSession,
    webhook_client: AsyncClient,
    webhook_headers: dict[str, str],
    seed_owner: Owner,
) -> None:
    # Open menu
    resp = await webhook_client.post(
        "/telegram/webhook",
        json=_menu_command_payload(OWNER_TG_ID),
        headers=webhook_headers,
    )
    assert resp.json()["status"] == "ok"

    # Click add supplier
    payload = _callback_payload(
        OWNER_TG_ID,
        "cb2",
        CallbackData(namespace="admin", action="supplier_add_start"),
    )
    resp = await webhook_client.post("/telegram/webhook", json=payload, headers=webhook_headers)
    assert resp.json()["status"] == "ok"

    dialog = await db_session.get(AdminDialog, OWNER_TG_ID)
    assert dialog is not None
    assert dialog.state == "await_supplier_name"

    # Send name
    text_payload = {
        "update_id": 101,
        "message": {
            "message_id": 2,
            "from": {"id": OWNER_TG_ID},
            "chat": {"id": OWNER_TG_ID, "type": "private"},
            "text": "New Supplier",
        },
    }
    resp = await webhook_client.post(
        "/telegram/webhook", json=text_payload, headers=webhook_headers
    )
    assert resp.json()["status"] == "ok"

    supplier = (
        await db_session.execute(
            select(Supplier).where(Supplier.name == "New Supplier")
        )
    ).scalar_one_or_none()
    assert supplier is not None
    assert supplier.active is True

    dialog = await db_session.get(AdminDialog, OWNER_TG_ID)
    assert dialog is None


@pytest.mark.asyncio
async def test_toggle_supplier_rfq(
    db_session: AsyncSession,
    webhook_client: AsyncClient,
    webhook_headers: dict[str, str],
    seed_owner: Owner,
    seed_suppliers: list[Supplier],
) -> None:
    supplier = seed_suppliers[0]
    payload = _callback_payload(
        OWNER_TG_ID,
        "cb",
        CallbackData(namespace="admin", action="supplier_toggle_rfq", arg=supplier.id),
    )
    resp = await webhook_client.post("/telegram/webhook", json=payload, headers=webhook_headers)
    assert resp.json()["status"] == "ok"

    await db_session.refresh(supplier)
    assert supplier.rfq_enabled is False


@pytest.mark.asyncio
async def test_set_default_chat(
    db_session: AsyncSession,
    webhook_client: AsyncClient,
    webhook_headers: dict[str, str],
    seed_owner: Owner,
    seed_suppliers: list[Supplier],
) -> None:
    supplier = seed_suppliers[0]
    chat = SupplierChat(
        supplier_id=supplier.id,
        chat_id=-100500,
        chat_type=SupplierChatType.supergroup,
        active=True,
    )
    db_session.add(chat)
    await db_session.flush()

    payload = _callback_payload(
        OWNER_TG_ID,
        "cb",
        CallbackData(
            namespace="admin",
            action="supplier_set_default",
            arg=supplier.id,
            page=chat.chat_id,
        ),
    )
    resp = await webhook_client.post("/telegram/webhook", json=payload, headers=webhook_headers)
    assert resp.json()["status"] == "ok"

    await db_session.refresh(chat)
    assert chat.is_default is True


@pytest.mark.asyncio
async def test_idempotent_callback(
    webhook_client: AsyncClient,
    webhook_headers: dict[str, str],
    seed_owner: Owner,
) -> None:
    payload = _callback_payload(
        OWNER_TG_ID,
        "cb",
        CallbackData(namespace="admin", action="main_menu"),
    )
    resp1 = await webhook_client.post("/telegram/webhook", json=payload, headers=webhook_headers)
    resp2 = await webhook_client.post("/telegram/webhook", json=payload, headers=webhook_headers)
    assert resp1.json()["status"] == "ok"
    assert resp2.json()["status"] == "duplicate"


class _FakeParserChannel:
    def __init__(
        self,
        id: int,
        channel_id: int | None,
        username: str | None,
        title: str | None,
        is_active: bool,
        *,
        purpose: str = "supplier_price_source",
    ):
        self.id = id
        self.channel_id = channel_id
        self.username = username
        self.title = title
        self.is_active = is_active
        self.purpose = purpose
        self.status = "active" if is_active else "pending"


@pytest.mark.asyncio
async def test_price_channel_delete_callback(
    webhook_client: AsyncClient,
    webhook_headers: dict[str, str],
    seed_owner: Owner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deleted_ids: list[int] = []
    listed: list[int] = []

    async def _delete(channel_id: int) -> None:
        deleted_ids.append(channel_id)

    async def _list() -> list[_FakeParserChannel]:
        listed.append(1)
        return [_FakeParserChannel(7, -100500, "@prices", "Prices", True)]

    monkeypatch.setattr(parser_client, "delete_channel", _delete)
    monkeypatch.setattr(parser_client, "list_channels", _list)

    payload = _callback_payload(
        OWNER_TG_ID,
        "cb_del",
        CallbackData(namespace="admin", action="price_channel_delete", arg=7),
    )
    resp = await webhook_client.post("/telegram/webhook", json=payload, headers=webhook_headers)
    assert resp.json()["status"] == "ok"
    assert deleted_ids == [7]
    assert listed == [1, 1]  # list before + after delete


@pytest.mark.asyncio
async def test_price_channel_add_dialog(
    webhook_client: AsyncClient,
    webhook_headers: dict[str, str],
    seed_owner: Owner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    added: list[str] = []

    async def _list() -> list[_FakeParserChannel]:
        return []

    async def _add(handle: str) -> object:
        added.append(handle)
        return _FakeParserChannel(3, -100300, handle, "New", True)

    monkeypatch.setattr(parser_client, "list_channels", _list)
    monkeypatch.setattr(parser_client, "add_channel", _add)

    # Start add dialog
    resp = await webhook_client.post(
        "/telegram/webhook",
        json=_callback_payload(
            OWNER_TG_ID,
            "cb_add",
            CallbackData(namespace="admin", action="price_channel_add_start"),
        ),
        headers=webhook_headers,
    )
    assert resp.json()["status"] == "ok"

    # Send channel handle
    text_payload = {
        "update_id": 101,
        "message": {
            "message_id": 2,
            "from": {"id": OWNER_TG_ID},
            "chat": {"id": OWNER_TG_ID, "type": "private"},
            "text": "@supplier_prices",
        },
    }
    resp = await webhook_client.post(
        "/telegram/webhook", json=text_payload, headers=webhook_headers
    )
    assert resp.json()["status"] == "ok"
    assert added == ["@supplier_prices"]


@pytest.mark.asyncio
async def test_bind_supplier_price_channel_stores_telegram_channel_id(
    webhook_client: AsyncClient,
    webhook_headers: dict[str, str],
    db_session: AsyncSession,
    seed_owner: Owner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supplier = Supplier(name="Bind Test", active=True, rfq_enabled=True)
    db_session.add(supplier)
    await db_session.flush()

    async def _list() -> list[_FakeParserChannel]:
        return [
            _FakeParserChannel(5, -1007777777777, "@ready_prices", "Ready", True),
        ]

    monkeypatch.setattr(parser_client, "list_channels", _list)

    resp = await webhook_client.post(
        "/telegram/webhook",
        json=_callback_payload(
            OWNER_TG_ID,
            "cb_bind",
            CallbackData(
                namespace="admin",
                action="supplier_bind_ch",
                arg=supplier.id,
                page=5,
            ),
        ),
        headers=webhook_headers,
    )
    assert resp.json()["status"] == "ok"
    await db_session.refresh(supplier)
    assert supplier.price_channel_id == -1007777777777
    assert supplier.price_channel_username == "@ready_prices"


@pytest.mark.asyncio
async def test_bind_pending_channel_refused(
    webhook_client: AsyncClient,
    webhook_headers: dict[str, str],
    db_session: AsyncSession,
    seed_owner: Owner,
    mock_telegram,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supplier = Supplier(name="Pending Test", active=True, rfq_enabled=True)
    db_session.add(supplier)
    await db_session.flush()

    async def _list() -> list[_FakeParserChannel]:
        return [
            _FakeParserChannel(9, None, "@pending", "Pending", False),
        ]

    monkeypatch.setattr(parser_client, "list_channels", _list)

    resp = await webhook_client.post(
        "/telegram/webhook",
        json=_callback_payload(
            OWNER_TG_ID,
            "cb_pending",
            CallbackData(
                namespace="admin",
                action="supplier_bind_ch",
                arg=supplier.id,
                page=9,
            ),
        ),
        headers=webhook_headers,
    )
    assert resp.json()["status"] == "ok"
    await db_session.refresh(supplier)
    assert supplier.price_channel_id is None
    last_text = mock_telegram.edited[-1][2] if mock_telegram.edited else mock_telegram.sent[-1][1]
    assert "не готов" in last_text


@pytest.mark.asyncio
async def test_bind_channel_busy_refused(
    webhook_client: AsyncClient,
    webhook_headers: dict[str, str],
    db_session: AsyncSession,
    seed_owner: Owner,
    mock_telegram,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    taken = Supplier(
        name="Taken",
        active=True,
        price_channel_id=-1008888888888,
        price_channel_username="@busy",
    )
    target = Supplier(name="Target", active=True, rfq_enabled=True)
    db_session.add_all([taken, target])
    await db_session.flush()

    async def _list() -> list[_FakeParserChannel]:
        return [
            _FakeParserChannel(11, -1008888888888, "@busy", "Busy", True),
        ]

    monkeypatch.setattr(parser_client, "list_channels", _list)

    resp = await webhook_client.post(
        "/telegram/webhook",
        json=_callback_payload(
            OWNER_TG_ID,
            "cb_busy",
            CallbackData(
                namespace="admin",
                action="supplier_bind_ch",
                arg=target.id,
                page=11,
            ),
        ),
        headers=webhook_headers,
    )
    assert resp.json()["status"] == "ok"
    await db_session.refresh(target)
    assert target.price_channel_id is None
    last_text = mock_telegram.edited[-1][2] if mock_telegram.edited else mock_telegram.sent[-1][1]
    assert "другому поставщику" in last_text


@pytest.mark.asyncio
async def test_unbind_supplier_price_channel(
    webhook_client: AsyncClient,
    webhook_headers: dict[str, str],
    db_session: AsyncSession,
    seed_owner: Owner,
) -> None:
    supplier = Supplier(
        name="Unbind",
        active=True,
        price_channel_id=-100111,
        price_channel_username="@x",
    )
    db_session.add(supplier)
    await db_session.flush()

    resp = await webhook_client.post(
        "/telegram/webhook",
        json=_callback_payload(
            OWNER_TG_ID,
            "cb_unbind",
            CallbackData(namespace="admin", action="supplier_unbind_ch", arg=supplier.id),
        ),
        headers=webhook_headers,
    )
    assert resp.json()["status"] == "ok"
    await db_session.refresh(supplier)
    assert supplier.price_channel_id is None
    assert supplier.price_channel_username is None


@pytest.mark.asyncio
async def test_set_supplier_telegram_id_and_start_dm_ok(
    webhook_client: AsyncClient,
    webhook_headers: dict[str, str],
    db_session: AsyncSession,
    seed_owner: Owner,
) -> None:
    supplier = Supplier(name="TG Supplier", active=True, rfq_enabled=True)
    db_session.add(supplier)
    await db_session.flush()
    new_tgid = 400400400

    await webhook_client.post(
        "/telegram/webhook",
        json=_callback_payload(
            OWNER_TG_ID,
            "cb_tgid",
            CallbackData(namespace="admin", action="supplier_tgid_start", arg=supplier.id),
        ),
        headers=webhook_headers,
    )
    resp = await webhook_client.post(
        "/telegram/webhook",
        json=_owner_text_payload(
            OWNER_TG_ID,
            update_id=2001,
            text=str(new_tgid),
        ),
        headers=webhook_headers,
    )
    assert resp.json()["status"] == "ok"
    await db_session.refresh(supplier)
    assert supplier.telegram_id == new_tgid

    chat = await db_session.scalar(
        select(SupplierChat).where(
            SupplierChat.supplier_id == supplier.id,
            SupplierChat.chat_id == new_tgid,
        )
    )
    assert chat is not None
    assert chat.active is True

    start_resp = await webhook_client.post(
        "/telegram/webhook",
        json={
            "update_id": 2002,
            "message": {
                "message_id": 20,
                "from": {"id": new_tgid},
                "chat": {"id": new_tgid, "type": "private"},
                "text": "/start",
            },
        },
        headers=webhook_headers,
    )
    assert start_resp.json()["status"] == "ok"
    await db_session.refresh(supplier)
    assert supplier.dm_ok is True


@pytest.mark.asyncio
async def test_clear_supplier_telegram_id_deactivates_private_chat(
    webhook_client: AsyncClient,
    webhook_headers: dict[str, str],
    db_session: AsyncSession,
    seed_owner: Owner,
) -> None:
    tgid = 400400401
    supplier = Supplier(
        name="Clear TG",
        active=True,
        telegram_id=tgid,
        dm_ok=True,
    )
    db_session.add(supplier)
    await db_session.flush()
    chat = SupplierChat(
        supplier_id=supplier.id,
        chat_id=tgid,
        chat_type=SupplierChatType.private,
        is_default=True,
        active=True,
    )
    db_session.add(chat)
    await db_session.flush()

    resp = await webhook_client.post(
        "/telegram/webhook",
        json=_callback_payload(
            OWNER_TG_ID,
            "cb_clear",
            CallbackData(namespace="admin", action="supplier_tgid_clear", arg=supplier.id),
        ),
        headers=webhook_headers,
    )
    assert resp.json()["status"] == "ok"
    await db_session.refresh(supplier)
    await db_session.refresh(chat)
    assert supplier.telegram_id is None
    assert supplier.dm_ok is False
    assert chat.active is False
    assert chat.is_default is False
