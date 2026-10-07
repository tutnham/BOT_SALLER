"""Product-aware binding (plan §11.7–10)."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Owner, Quote
from app.handlers.supplier_messages import handle_reply
from app.parsers.product_normalizer import extract_product_attrs
from app.services.request_service import create_request
from tests.conftest import MockTelegramClient

pytestmark = pytest.mark.usefixtures("supplier_single_candidate_auto_bind")


def test_brandless_18_pro_max_extracts_number_variant() -> None:
    attrs = extract_product_attrs("18 Pro Max")
    assert attrs.number == "18"
    assert attrs.variant == "pro max"


@pytest.mark.asyncio
async def test_one_request_bare_127500_auto_binds(
    db_session: AsyncSession,
    seed_employee,
    seed_suppliers,
    seed_client_group,
    mock_telegram: MockTelegramClient,
) -> None:
    request, _ = await create_request(
        db_session,
        group_chat_id=seed_client_group.chat_id,
        employee_id=seed_employee.id,
        source_text="iPhone 17 Pro Max 1TB Silver",
        telegram=mock_telegram,
    )
    supplier = seed_suppliers[0]
    assert supplier.telegram_id is not None
    await handle_reply(
        db_session,
        {
            "message_id": 9001,
            "chat": {"id": supplier.telegram_id, "type": "private"},
            "from": {"id": supplier.telegram_id},
            "text": "127500",
        },
        telegram=mock_telegram,
    )
    quote = await db_session.scalar(
        select(Quote).where(
            Quote.request_id == request.id,
            Quote.supplier_id == supplier.id,
        )
    )
    assert quote is not None
    assert quote.price_initial == 127500


@pytest.mark.asyncio
async def test_iphone_18_on_iphone_17_request_no_auto_quote(
    db_session: AsyncSession,
    seed_employee,
    seed_suppliers,
    seed_client_group,
    mock_telegram: MockTelegramClient,
) -> None:
    owner = Owner(telegram_id=300300501, name="Review Owner", dm_ok=True)
    db_session.add(owner)
    await db_session.flush()

    await create_request(
        db_session,
        group_chat_id=seed_client_group.chat_id,
        employee_id=seed_employee.id,
        source_text="iPhone 17 Pro Max 1TB Silver",
        telegram=mock_telegram,
    )
    supplier = seed_suppliers[0]
    assert supplier.telegram_id is not None
    mock_telegram.sent.clear()

    await handle_reply(
        db_session,
        {
            "message_id": 9002,
            "chat": {"id": supplier.telegram_id, "type": "private"},
            "from": {"id": supplier.telegram_id},
            "text": "18 Pro Max 256GB Glacier Esim 127500",
        },
        telegram=mock_telegram,
    )
    quotes = (await db_session.execute(select(Quote))).scalars().all()
    assert quotes == []
    assert any(item[0] == owner.telegram_id for item in mock_telegram.sent)


@pytest.mark.asyncio
async def test_brandless_17_pro_max_auto_binds_matching_request(
    db_session: AsyncSession,
    seed_employee,
    seed_suppliers,
    seed_client_group,
    mock_telegram: MockTelegramClient,
) -> None:
    request, _ = await create_request(
        db_session,
        group_chat_id=seed_client_group.chat_id,
        employee_id=seed_employee.id,
        source_text="iPhone 17 Pro Max 256GB",
        telegram=mock_telegram,
    )
    supplier = seed_suppliers[0]
    assert supplier.telegram_id is not None
    await handle_reply(
        db_session,
        {
            "message_id": 9003,
            "chat": {"id": supplier.telegram_id, "type": "private"},
            "from": {"id": supplier.telegram_id},
            "text": "17 Pro Max 256GB 127500",
        },
        telegram=mock_telegram,
    )
    quote = await db_session.scalar(
        select(Quote).where(
            Quote.request_id == request.id,
            Quote.supplier_id == supplier.id,
        )
    )
    assert quote is not None
