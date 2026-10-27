"""Edited Business message: supersede old Jev decision, reclassify new text (plan §5 cases 31-35)."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import (
    BusinessConnection,
    MessageKind,
    MessageOut,
    Quote,
    SupplierAiDecision,
)
from app.handlers.business_events import handle_edited_business_message
from app.handlers.supplier_messages import handle_reply
from app.integrations.jev.client import set_jev_client
from app.integrations.jev.models import JevCandidate, JevProviderResponse
from app.services.request_service import create_request
from tests.conftest import MockTelegramClient


class _AcceptFirst:
    async def classify_supplier_message(
        self,
        *,
        message_text: str,
        candidates: list[JevCandidate],
        correlation_id: str,
    ) -> JevProviderResponse:
        del message_text, correlation_id
        choice = candidates[0].candidate_id
        return JevProviderResponse(
            model="jev-1.14.0",
            message_choice="price_offer",
            price_probability=Decimal("0.99"),
            binding_choice=choice,
            binding_probabilities={
                choice: Decimal("0.99"),
                "none": Decimal("0.005"),
                "ambiguous": Decimal("0.005"),
            },
            binding_confidence=Decimal("0.98"),
        )


def _enable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JEV_ENABLED", "true")
    monkeypatch.setenv("JEV_API_KEY", "test-jev-key")
    monkeypatch.setenv("JEV_SHADOW_MODE", "false")
    monkeypatch.setenv("JEV_FALLBACK_TO_EXISTING_LLM", "false")
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_jev() -> None:
    yield
    set_jev_client(None)
    get_settings.cache_clear()


def _business_message(supplier_telegram_id: int, message_id: int, text: str) -> dict:
    return {
        "message_id": message_id,
        "chat": {"id": supplier_telegram_id, "type": "private"},
        "from": {"id": supplier_telegram_id},
        "text": text,
    }


async def _seed_business_route(
    db_session: AsyncSession,
    *,
    supplier,
    request_id: int,
    connection_id: str = "bc_jev",
) -> None:
    """Business route is eligible only when an outbound ask was sent over it."""
    db_session.add(
        BusinessConnection(
            tg_user_id=910123,
            business_connection_id=connection_id,
            can_reply=True,
            can_read_messages=True,
            is_enabled=True,
        )
    )
    db_session.add(
        MessageOut(
            request_id=request_id,
            supplier_id=supplier.id,
            tg_message_id=123,
            chat_id=supplier.telegram_id,
            text="ask",
            kind=MessageKind.ask,
            send_status="sent",
            business_connection_id=connection_id,
        )
    )
    await db_session.flush()


@pytest.mark.asyncio
async def test_edited_business_message_supersedes_and_reclassifies(
    db_session: AsyncSession,
    seed_employee,
    seed_suppliers,
    seed_client_group,
    mock_telegram: MockTelegramClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    set_jev_client(_AcceptFirst())
    request, _ = await create_request(
        db_session,
        group_chat_id=seed_client_group.chat_id,
        employee_id=seed_employee.id,
        source_text="iPhone 17 Pro 256 Blue",
        telegram=mock_telegram,
    )
    supplier = seed_suppliers[0]
    assert supplier.telegram_id is not None
    await _seed_business_route(db_session, supplier=supplier, request_id=request.id)

    status = await handle_reply(
        db_session,
        _business_message(supplier.telegram_id, 8805, "117900"),
        telegram=mock_telegram,
        business_connection_id="bc_jev",
    )
    assert status == "ok"

    rows = (
        (await db_session.execute(select(SupplierAiDecision).order_by(SupplierAiDecision.id)))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    first = rows[0]
    assert first.superseded_at is None
    assert first.is_current is True

    # Edited message: old decision must be superseded, new text reclassified.
    edit_status = await handle_edited_business_message(
        db_session,
        {
            "business_connection_id": "bc_jev",
            "chat": {"id": supplier.telegram_id},
            "message_id": 8805,
            "text": "118000",
        },
        telegram=mock_telegram,
    )
    assert edit_status == "ok"

    rows = (
        (await db_session.execute(select(SupplierAiDecision).order_by(SupplierAiDecision.id)))
        .scalars()
        .all()
    )
    assert len(rows) == 2
    assert rows[0].superseded_at is not None
    assert rows[0].is_current is False
    assert rows[1].superseded_at is None
    assert rows[1].is_current is True
    assert rows[1].input_hash != rows[0].input_hash

    # Edit corrects the existing quote, it must not create a second quote.
    quotes = (await db_session.execute(select(func.count()).select_from(Quote))).scalar_one()
    assert quotes == 1


@pytest.mark.asyncio
async def test_edited_business_message_without_jev_still_supersedes(
    db_session: AsyncSession,
    seed_employee,
    seed_suppliers,
    seed_client_group,
    mock_telegram: MockTelegramClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    set_jev_client(_AcceptFirst())
    request, _ = await create_request(
        db_session,
        group_chat_id=seed_client_group.chat_id,
        employee_id=seed_employee.id,
        source_text="iPhone 17 Pro 256 Blue",
        telegram=mock_telegram,
    )
    supplier = seed_suppliers[0]
    assert supplier.telegram_id is not None
    await _seed_business_route(db_session, supplier=supplier, request_id=request.id)
    await handle_reply(
        db_session,
        _business_message(supplier.telegram_id, 8806, "117900"),
        telegram=mock_telegram,
        business_connection_id="bc_jev",
    )

    # Kill switch mid-flight: edit must supersede old decisions without new Jev calls.
    monkeypatch.setenv("JEV_ENABLED", "false")
    get_settings.cache_clear()
    set_jev_client(_AcceptFirst())

    edit_status = await handle_edited_business_message(
        db_session,
        {
            "business_connection_id": "bc_jev",
            "chat": {"id": supplier.telegram_id},
            "message_id": 8806,
            "text": "118500",
        },
        telegram=mock_telegram,
    )
    assert edit_status == "ok"

    rows = (
        (await db_session.execute(select(SupplierAiDecision).order_by(SupplierAiDecision.id)))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].superseded_at is not None
    assert rows[0].is_current is False