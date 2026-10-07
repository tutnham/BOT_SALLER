from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Deal, DealOutcome, Request, RequestStatus, Supplier
from app.services.purchase_report_service import (
    _csv_cell,
    build_purchase_report_csv,
    build_purchase_report_telegram,
)


@pytest.mark.asyncio
async def test_purchase_report_command_requires_role(
    db_session: AsyncSession,
) -> None:
    from app.handlers.purchase_report_commands import handle_purchase_report_command
    from tests.conftest import MockTelegramClient

    telegram = MockTelegramClient()
    consumed = await handle_purchase_report_command(
        db_session,
        text="/purchase_report today",
        chat_id=1,
        telegram_id=111,
        telegram=telegram,
    )
    assert consumed is True
    assert telegram.sent == []


def test_csv_formula_injection_prefix() -> None:
    assert _csv_cell("=cmd") == "'=cmd"
    assert _csv_cell("+1+1") == "'+1+1"
    assert _csv_cell("17 Air") == "17 Air"


@pytest.mark.asyncio
async def test_report_reads_deals_not_quotes(
    db_session: AsyncSession,
    seed_employee,
    seed_client_group,
    seed_suppliers: list[Supplier],
    seed_owner,
) -> None:
    request = Request(
        group_chat_id=seed_client_group.chat_id,
        employee_id=seed_employee.id,
        source_text="17 Air 256GB Black",
        status=RequestStatus.closed,
        canonical_sku_key="v1|apple|iphone|17-air|256|black|*|*",
        requested_qty=1,
    )
    db_session.add(request)
    await db_session.flush()
    db_session.add(
        Deal(
            request_id=request.id,
            chosen_supplier_id=seed_suppliers[0].id,
            final_price=Decimal("92000"),
            outcome=DealOutcome.won,
            closed_at=datetime.now(UTC),
            purchased_qty=1,
            client_unit_price=Decimal("92500"),
            recorded_by_employee_id=seed_employee.id,
        )
    )
    await db_session.flush()
    text = await build_purchase_report_telegram(db_session, period="day")
    assert "17 Air 256GB Black" in text
    assert "92000" in text
    assert seed_suppliers[0].name in text
    start = datetime.now(UTC) - timedelta(days=1)
    end = datetime.now(UTC) + timedelta(days=1)
    csv_body = await build_purchase_report_csv(db_session, start=start, end=end)
    assert "92000" in csv_body
    assert "92500" in csv_body
