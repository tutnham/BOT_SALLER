"""Multiline batch reply: Jev per line, lines stay independent (plan §5 cases 36-40).

Invariant under test: a bare price with several candidates is NEVER auto-bound
(``multiple_candidates_bare_price``) — per line, regardless of provider confidence.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import MessageIn, Quote, SupplierAiDecision, SupplierMessageItem
from app.integrations.jev.client import set_jev_client
from app.integrations.jev.models import JevCandidate, JevProviderResponse
from app.services.request_service import create_request
from app.services.supplier_batch_service import process_batch_supplier_reply
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


def _enable(monkeypatch: pytest.MonkeyPatch, *, shadow: bool) -> None:
    monkeypatch.setenv("JEV_ENABLED", "true")
    monkeypatch.setenv("JEV_API_KEY", "test-jev-key")
    monkeypatch.setenv("JEV_SHADOW_MODE", "true" if shadow else "false")
    monkeypatch.setenv("JEV_FALLBACK_TO_EXISTING_LLM", "false")
    monkeypatch.setenv("SUPPLIER_BATCH_REPLY_ENABLED", "true")
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_jev() -> None:
    yield
    set_jev_client(None)
    get_settings.cache_clear()


async def _seed_two_requests(db_session: AsyncSession, mock_telegram, seed_employee, seed_client_group):
    request_a, _ = await create_request(
        db_session,
        group_chat_id=seed_client_group.chat_id,
        employee_id=seed_employee.id,
        source_text="iPhone 15 128 Black",
        telegram=mock_telegram,
    )
    request_b, _ = await create_request(
        db_session,
        group_chat_id=seed_client_group.chat_id,
        employee_id=seed_employee.id,
        source_text="iPhone 17 Pro 256 Blue",
        telegram=mock_telegram,
    )
    return request_a, request_b


@pytest.mark.asyncio
async def test_multiline_bare_price_never_auto_binds_even_active(
    db_session: AsyncSession,
    seed_employee,
    seed_suppliers,
    seed_client_group,
    mock_telegram: MockTelegramClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch, shadow=False)
    set_jev_client(_AcceptFirst())
    await _seed_two_requests(db_session, mock_telegram, seed_employee, seed_client_group)
    supplier = seed_suppliers[0]
    assert supplier.telegram_id is not None

    message_in = MessageIn(
        supplier_id=supplier.id,
        chat_id=supplier.telegram_id,
        tg_message_id=9901,
        raw_text="70000\n118000",
        bind_method="none",
        bind_status="unbound",
    )
    db_session.add(message_in)
    await db_session.flush()

    status = await process_batch_supplier_reply(
        db_session,
        supplier=supplier,
        raw_text="70000\n118000",
        message_in=message_in,
        chat_id=supplier.telegram_id,
        business_connection_id=None,
        telegram=mock_telegram,
    )
    assert status == "ok"

    # Stage-3 guard: bare price with several candidates goes to review, per line.
    quotes = (await db_session.execute(select(func.count()).select_from(Quote))).scalar_one()
    assert quotes == 0

    decisions = (
        (
            await db_session.execute(
                select(SupplierAiDecision).order_by(SupplierAiDecision.line_no)
            )
        )
        .scalars()
        .all()
    )
    assert len(decisions) == 2
    assert [row.line_no for row in decisions] == [1, 2]
    assert all(row.recommended_action == "review" for row in decisions)
    assert all(
        row.reason_code == "multiple_candidates_bare_price" for row in decisions
    )
    # Provider wanted candidate[0] with 0.99 confidence — stored, not applied.
    assert all(row.selected_request_id is None for row in decisions)

    items = (await db_session.execute(select(SupplierMessageItem))).scalars().all()
    assert len(items) == 2
    assert all(item.bind_status != "resolved" for item in items)


@pytest.mark.asyncio
async def test_multiline_shadow_records_decisions_without_quotes(
    db_session: AsyncSession,
    seed_employee,
    seed_suppliers,
    seed_client_group,
    mock_telegram: MockTelegramClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch, shadow=True)
    set_jev_client(_AcceptFirst())
    await _seed_two_requests(db_session, mock_telegram, seed_employee, seed_client_group)
    supplier = seed_suppliers[0]
    assert supplier.telegram_id is not None

    message_in = MessageIn(
        supplier_id=supplier.id,
        chat_id=supplier.telegram_id,
        tg_message_id=9902,
        raw_text="70000\n118000",
        bind_method="none",
        bind_status="unbound",
    )
    db_session.add(message_in)
    await db_session.flush()

    status = await process_batch_supplier_reply(
        db_session,
        supplier=supplier,
        raw_text="70000\n118000",
        message_in=message_in,
        chat_id=supplier.telegram_id,
        business_connection_id=None,
        telegram=mock_telegram,
    )
    assert status == "ok"

    quotes = (await db_session.execute(select(Quote))).scalars().all()
    assert quotes == []
    decisions = (
        (await db_session.execute(select(SupplierAiDecision))).scalars().all()
    )
    assert len(decisions) == 2
    assert all(row.shadow_mode is True for row in decisions)
    assert all(
        row.reason_code == "multiple_candidates_bare_price" for row in decisions
    )