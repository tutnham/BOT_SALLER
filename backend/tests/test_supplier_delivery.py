"""Outbound delivery: business_connection_id payload, 400 peer miss, batch continue."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    BusinessConnection,
    Employee,
    MessageKind,
    MessageOut,
    Quote,
    QuoteSource,
    Request,
    RequestStatus,
    Supplier,
    SupplierChat,
    SupplierChatType,
)
from app.services.admin_service import add_supplier
from app.services.app_settings_service import (
    PRIORITY_BUSINESS_DM_FIRST,
    ROUTING_BUSINESS_DM_PRIORITY_KEY,
    set_setting,
)
from app.services.recheck_service import send_due_rechecks
from app.services.request_service import create_request
from app.telegram.client import (
    TelegramClient,
    TelegramSendError,
    is_business_peer_missing,
)


@pytest.mark.asyncio
async def test_send_message_includes_business_connection_id() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["payload"] = json.loads(request.content)
        return httpx.Response(
            status_code=200,
            json={"ok": True, "result": {"message_id": 42}},
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = TelegramClient(bot_token="token", http_client=http_client)
        message_id = await client.send_message(
            100,
            "hello",
            business_connection_id="bc_payload",
        )
    assert message_id == 42
    assert seen["payload"]["business_connection_id"] == "bc_payload"
    assert seen["payload"]["chat_id"] == 100


@pytest.mark.asyncio
async def test_is_business_peer_missing_matches_400_substring() -> None:
    assert is_business_peer_missing(
        TelegramSendError(
            "Bad Request: the user hasn't recently contacted us",
            status_code=400,
        )
    )
    assert is_business_peer_missing(
        TelegramSendError("BUSINESS_PEER_USAGE_MISSING", status_code=400)
    )
    assert not is_business_peer_missing(TelegramSendError("chat not found", status_code=400))
    assert not is_business_peer_missing(
        TelegramSendError("the user hasn't recently contacted us", status_code=403)
    )


class _FailPeerTelegram:
    def __init__(self, fail_ids: set[int]) -> None:
        self.sent: list[tuple[int, str]] = []
        self.fail_ids = fail_ids

    async def send_message(self, chat_id: int, text: str, **kwargs: object) -> int:
        if chat_id in self.fail_ids:
            raise TelegramSendError(
                "Bad Request: the user hasn't recently contacted us",
                status_code=400,
            )
        self.sent.append((chat_id, text))
        return len(self.sent) + 100


@pytest.mark.asyncio
async def test_ask_partial_failure_continues_and_notifies_group(
    db_session: AsyncSession,
    seed_employee: Employee,
    seed_group_chat_id: int,
    seed_suppliers: list[Supplier],
) -> None:
    fail_id = seed_suppliers[0].telegram_id or 0
    telegram = _FailPeerTelegram({fail_id})
    request, sent_count = await create_request(
        db_session,
        group_chat_id=seed_group_chat_id,
        employee_id=seed_employee.id,
        source_text="iPhone 17",
        telegram=telegram,
    )
    assert sent_count >= 1
    failed = (
        await db_session.execute(
            select(MessageOut).where(
                MessageOut.request_id == request.id,
                MessageOut.send_status == "failed",
            )
        )
    ).scalars().all()
    assert len(failed) == 1
    assert failed[0].error_text is not None
    assert "recently" in failed[0].error_text.lower()
    assert any("не доставлено" in text for _, text in telegram.sent)


@pytest.mark.asyncio
async def test_recheck_peer_missing_does_not_retry(
    db_session: AsyncSession,
    seed_employee: Employee,
) -> None:
    supplier = await add_supplier(db_session, name="RecheckBiz", telegram_id=640001)
    chats = (
        await db_session.execute(
            select(SupplierChat).where(SupplierChat.supplier_id == supplier.id)
        )
    ).scalars().all()
    for chat in chats:
        if chat.chat_type is SupplierChatType.private:
            chat.active = False
            chat.is_default = False
    db_session.add(
        BusinessConnection(
            tg_user_id=812001,
            business_connection_id="bc_recheck",
            can_reply=True,
            can_read_messages=True,
            is_enabled=True,
        )
    )
    db_session.add(
        SupplierChat(
            supplier_id=supplier.id,
            chat_id=640001,
            chat_type=SupplierChatType.business_dm,
            business_connection_id="bc_recheck",
            active=True,
            is_default=False,
        )
    )
    await set_setting(
        db_session, ROUTING_BUSINESS_DM_PRIORITY_KEY, PRIORITY_BUSINESS_DM_FIRST
    )
    request = Request(
        group_chat_id=-100640,
        employee_id=seed_employee.id,
        source_text="iPhone",
        status=RequestStatus.needs_recheck,
    )
    db_session.add(request)
    await db_session.flush()
    db_session.add(
        Quote(
            request_id=request.id,
            supplier_id=supplier.id,
            price_initial=Decimal("85000"),
            source=QuoteSource.manual,
            confidence=1.0,
        )
    )
    request.recheck_at = datetime.now(UTC) - timedelta(minutes=1)
    await db_session.flush()

    telegram = _FailPeerTelegram({640001})
    result = await send_due_rechecks(db_session, telegram)
    assert result["processed"] == 0
    assert result["skipped"] == 1
    await db_session.refresh(request)
    assert request.recheck_at is None
    outbound = await db_session.scalar(
        select(MessageOut).where(MessageOut.kind == MessageKind.recheck)
    )
    assert outbound is not None
    assert outbound.send_status == "failed"
    assert any("не доставлено" in text for _, text in telegram.sent)
