"""Inbound business_message: quotes, autobind, idempotency."""

from __future__ import annotations

from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    BusinessConnection,
    MessageIn,
    MessageKind,
    MessageOut,
    Quote,
    Request,
    RequestStatus,
    SupplierChat,
    SupplierChatType,
)
from app.services.admin_service import add_supplier


async def _seed_connection(
    db_session: AsyncSession,
    *,
    tg_user_id: int = 910001,
    connection_id: str = "bc_msg",
) -> BusinessConnection:
    row = BusinessConnection(
        tg_user_id=tg_user_id,
        business_connection_id=connection_id,
        can_reply=True,
        can_read_messages=True,
        is_enabled=True,
    )
    db_session.add(row)
    await db_session.flush()
    return row


def _business_message_update(
    *,
    update_id: int,
    from_id: int,
    text: str,
    connection_id: str = "bc_msg",
    reply_to_message_id: int | None = None,
    reply_to_text: str | None = None,
    sender_business_bot: bool = False,
) -> dict:
    message: dict = {
        "message_id": update_id * 10,
        "from": {"id": from_id, "first_name": "Supplier"},
        "chat": {"id": from_id, "type": "private"},
        "text": text,
        "business_connection_id": connection_id,
    }
    if sender_business_bot:
        message["sender_business_bot"] = {"id": 1, "is_bot": True}
    if reply_to_message_id is not None:
        message["reply_to_message"] = {
            "message_id": reply_to_message_id,
            "from": {"id": 1, "is_bot": True},
            "text": reply_to_text or "",
        }
    return {"update_id": update_id, "business_message": message}


@pytest.mark.asyncio
async def test_business_message_reply_creates_quote(
    webhook_client: AsyncClient,
    db_session: AsyncSession,
    webhook_headers: dict[str, str],
    seed_employee,
    seed_group_chat_id: int,
) -> None:
    await _seed_connection(db_session)
    supplier = await add_supplier(db_session, name="Biz", telegram_id=620001)
    request = Request(
        group_chat_id=seed_group_chat_id,
        employee_id=seed_employee.id,
        source_text="iPhone 17",
        status=RequestStatus.awaiting_answers,
    )
    db_session.add(request)
    await db_session.flush()
    db_session.add(
        MessageOut(
            request_id=request.id,
            supplier_id=supplier.id,
            tg_message_id=1,
            chat_id=620001,
            text=f"Запрос #{request.id}",
            kind=MessageKind.ask,
            send_status="sent",
            business_connection_id="bc_msg",
        )
    )
    await db_session.flush()

    resp = await webhook_client.post(
        "/telegram/webhook",
        json=_business_message_update(
            update_id=62001,
            from_id=620001,
            text="Есть, 77000 руб",
            reply_to_message_id=1,
            reply_to_text=f"Запрос #{request.id}",
        ),
        headers=webhook_headers,
    )
    assert resp.json()["status"] == "ok"

    quote = await db_session.scalar(
        select(Quote).where(
            Quote.request_id == request.id,
            Quote.supplier_id == supplier.id,
        )
    )
    assert quote is not None
    assert quote.price_initial == Decimal("77000")
    msg_in = await db_session.scalar(select(MessageIn).where(MessageIn.supplier_id == supplier.id))
    assert msg_in is not None
    assert msg_in.business_connection_id == "bc_msg"

    chat = await db_session.scalar(
        select(SupplierChat).where(
            SupplierChat.supplier_id == supplier.id,
            SupplierChat.chat_type == SupplierChatType.business_dm,
        )
    )
    assert chat is not None
    assert chat.active is True
    assert chat.is_default is False


@pytest.mark.asyncio
@pytest.mark.usefixtures("supplier_single_candidate_auto_bind")
async def test_business_message_without_reply_binds_with_price(
    webhook_client: AsyncClient,
    db_session: AsyncSession,
    webhook_headers: dict[str, str],
    seed_employee,
    seed_group_chat_id: int,
) -> None:
    await _seed_connection(db_session)
    supplier = await add_supplier(db_session, name="Biz2", telegram_id=620002)
    request = Request(
        group_chat_id=seed_group_chat_id,
        employee_id=seed_employee.id,
        source_text="iPhone",
        status=RequestStatus.awaiting_answers,
    )
    db_session.add(request)
    await db_session.flush()
    db_session.add(
        MessageOut(
            request_id=request.id,
            supplier_id=supplier.id,
            tg_message_id=2,
            chat_id=620002,
            text="ask",
            kind=MessageKind.ask,
            send_status="sent",
            business_connection_id="bc_msg",
        )
    )
    await db_session.flush()

    resp = await webhook_client.post(
        "/telegram/webhook",
        json=_business_message_update(
            update_id=62002,
            from_id=620002,
            text="88000 руб",
        ),
        headers=webhook_headers,
    )
    assert resp.json()["status"] == "ok"
    quote = await db_session.scalar(select(Quote).where(Quote.request_id == request.id))
    assert quote is not None
    assert quote.price_initial == 88000


@pytest.mark.asyncio
async def test_business_message_unknown_supplier_ignored(
    webhook_client: AsyncClient,
    db_session: AsyncSession,
    webhook_headers: dict[str, str],
) -> None:
    await _seed_connection(db_session)
    resp = await webhook_client.post(
        "/telegram/webhook",
        json=_business_message_update(
            update_id=62003,
            from_id=999888777,
            text="есть 1000",
        ),
        headers=webhook_headers,
    )
    assert resp.json()["status"] == "ignored"
    assert await db_session.scalar(select(func.count()).select_from(MessageIn)) == 0


@pytest.mark.asyncio
async def test_business_message_client_own_ignored(
    webhook_client: AsyncClient,
    db_session: AsyncSession,
    webhook_headers: dict[str, str],
) -> None:
    await _seed_connection(db_session, tg_user_id=620099)
    resp = await webhook_client.post(
        "/telegram/webhook",
        json=_business_message_update(
            update_id=62004,
            from_id=620099,
            text="свой текст",
        ),
        headers=webhook_headers,
    )
    assert resp.json()["status"] == "ignored"


@pytest.mark.asyncio
async def test_business_message_idempotent_replay(
    webhook_client: AsyncClient,
    db_session: AsyncSession,
    webhook_headers: dict[str, str],
    seed_employee,
    seed_group_chat_id: int,
) -> None:
    await _seed_connection(db_session)
    supplier = await add_supplier(db_session, name="Biz3", telegram_id=620003)
    request = Request(
        group_chat_id=seed_group_chat_id,
        employee_id=seed_employee.id,
        source_text="iPhone",
        status=RequestStatus.awaiting_answers,
    )
    db_session.add(request)
    await db_session.flush()
    db_session.add(
        MessageOut(
            request_id=request.id,
            supplier_id=supplier.id,
            tg_message_id=3,
            chat_id=620003,
            text=f"Запрос #{request.id}",
            kind=MessageKind.ask,
            send_status="sent",
            business_connection_id="bc_msg",
        )
    )
    await db_session.flush()
    payload = _business_message_update(
        update_id=62005,
        from_id=620003,
        text="Есть, 66000 руб",
        reply_to_message_id=3,
        reply_to_text=f"Запрос #{request.id}",
    )
    first = await webhook_client.post(
        "/telegram/webhook", json=payload, headers=webhook_headers
    )
    assert first.json()["status"] == "ok"
    second = await webhook_client.post(
        "/telegram/webhook", json=payload, headers=webhook_headers
    )
    assert second.json()["status"] == "duplicate"
    count = await db_session.scalar(select(func.count()).select_from(Quote))
    assert count == 1
    in_count = await db_session.scalar(select(func.count()).select_from(MessageIn))
    assert in_count == 1


@pytest.mark.asyncio
async def test_business_message_without_request_stays_silent(
    webhook_client: AsyncClient,
    db_session: AsyncSession,
    webhook_headers: dict[str, str],
    mock_telegram,
) -> None:
    await _seed_connection(db_session)
    await add_supplier(db_session, name="BizSilent", telegram_id=620006)
    mock_telegram.sent.clear()

    resp = await webhook_client.post(
        "/telegram/webhook",
        json=_business_message_update(
            update_id=62006,
            from_id=620006,
            text="Привет, прайс на сегодня",
        ),
        headers=webhook_headers,
    )
    assert resp.json()["status"] == "ok"

    msg_in = await db_session.scalar(
        select(MessageIn).where(MessageIn.tg_message_id == 620060)
    )
    assert msg_in is not None
    assert msg_in.request_id is None
    assert msg_in.business_connection_id == "bc_msg"

    dm_sends = [txt for cid, txt, *_ in mock_telegram.sent if cid == 620006]
    assert dm_sends == []
