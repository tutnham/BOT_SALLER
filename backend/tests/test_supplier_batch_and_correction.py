"""Batch RFQ lines and correction window (plan §11.11–16)."""

from __future__ import annotations

import os

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Quote, SupplierMessageItem
from app.handlers.supplier_messages import handle_reply
from app.services.request_service import create_request
from tests.conftest import MockTelegramClient

pytestmark = pytest.mark.usefixtures("supplier_single_candidate_auto_bind")


@pytest.mark.asyncio
async def test_multiline_not_ignored_when_candidates_exist(
    db_session: AsyncSession,
    seed_employee,
    seed_suppliers,
    seed_client_group,
    mock_telegram: MockTelegramClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SUPPLIER_BATCH_REPLY_ENABLED", "false")
    from app.config import get_settings

    get_settings.cache_clear()

    await create_request(
        db_session,
        group_chat_id=seed_client_group.chat_id,
        employee_id=seed_employee.id,
        source_text="iPhone 17 Pro 256",
        telegram=mock_telegram,
    )
    supplier = seed_suppliers[0]
    assert supplier.telegram_id is not None
    text = "17 pro 256 85000\n17 pro max 256 90000\n17 pro 256 88000"
    await handle_reply(
        db_session,
        {
            "message_id": 9100,
            "chat": {"id": supplier.telegram_id, "type": "private"},
            "from": {"id": supplier.telegram_id},
            "text": text,
        },
        telegram=mock_telegram,
    )
    from app.db.models import MessageIn

    msg_in = await db_session.scalar(
        select(MessageIn).where(MessageIn.tg_message_id == 9100)
    )
    assert msg_in is not None
    assert msg_in.bind_status != "ignored"


@pytest.mark.asyncio
async def test_batch_creates_line_items(
    db_session: AsyncSession,
    seed_employee,
    seed_suppliers,
    seed_client_group,
    mock_telegram: MockTelegramClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SUPPLIER_BATCH_REPLY_ENABLED", "true")
    from app.config import get_settings

    get_settings.cache_clear()

    r1, _ = await create_request(
        db_session,
        group_chat_id=seed_client_group.chat_id,
        employee_id=seed_employee.id,
        source_text="iPhone 17 Pro 256",
        telegram=mock_telegram,
    )
    r2, _ = await create_request(
        db_session,
        group_chat_id=seed_client_group.chat_id,
        employee_id=seed_employee.id,
        source_text="iPhone 17 Pro Max 256",
        telegram=mock_telegram,
    )
    supplier = seed_suppliers[0]
    assert supplier.telegram_id is not None
    await handle_reply(
        db_session,
        {
            "message_id": 9101,
            "chat": {"id": supplier.telegram_id, "type": "private"},
            "from": {"id": supplier.telegram_id},
            "text": "17 Pro 256 85000\n17 Pro Max 256 90000",
        },
        telegram=mock_telegram,
    )
    items = (await db_session.execute(select(SupplierMessageItem))).scalars().all()
    assert len(items) >= 2
    quotes = (await db_session.execute(select(Quote))).scalars().all()
    assert len(quotes) >= 1
