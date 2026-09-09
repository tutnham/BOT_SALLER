"""Direct tests for recheck workflow including partial-send handling."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Employee,
    MessageKind,
    MessageOut,
    Quote,
    QuoteSource,
    Request,
    RequestStatus,
    Supplier,
)
from app.services.recheck_service import (
    InvalidRecheckHoursError,
    schedule_recheck,
    send_due_rechecks,
)
from app.telegram.client import TelegramSendError


class _MockTelegram:
    def __init__(self, *, fail_for: set[int] | None = None) -> None:
        self.sent: list[tuple[int, str]] = []
        self.fail_for = fail_for or set()

    async def send_message(self, chat_id: int, text: str, **kwargs: object) -> int:
        if chat_id in self.fail_for:
            raise TelegramSendError("network")
        self.sent.append((chat_id, text))
        return len(self.sent)


async def _due_request(
    db_session: AsyncSession,
    supplier: Supplier,
    seed_employee: Employee,
) -> Request:
    request = Request(
        group_chat_id=-100123,
        employee_id=seed_employee.id,
        source_text="iPhone 17 256GB",
        normalized_json={"model": "iPhone 17"},
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
    return request


@pytest.mark.asyncio
async def test_send_due_rechecks_processes_due(
    db_session: AsyncSession,
    seed_suppliers: list[Supplier],
    seed_employee: Employee,
) -> None:
    request = await _due_request(db_session, seed_suppliers[0], seed_employee)
    supplier = seed_suppliers[0]
    telegram = _MockTelegram()

    result = await send_due_rechecks(db_session, telegram)

    assert result["processed"] == 1
    assert result["skipped"] == 0
    assert result["failed"] == 0
    assert telegram.sent == [(supplier.telegram_id, telegram.sent[0][1])]
    await db_session.refresh(request)
    assert request.recheck_at is None

    out = await db_session.scalar(
        select(MessageOut).where(MessageOut.kind == MessageKind.recheck)
    )
    assert out is not None
    assert out.supplier_id == supplier.id


@pytest.mark.asyncio
async def test_send_due_rechecks_partial_failure(
    db_session: AsyncSession,
    seed_suppliers: list[Supplier],
    seed_employee: Employee,
) -> None:
    request1 = await _due_request(db_session, seed_suppliers[0], seed_employee)
    request2 = await _due_request(db_session, seed_suppliers[1], seed_employee)
    fail_for = {seed_suppliers[0].telegram_id}
    telegram = _MockTelegram(fail_for=fail_for)

    result = await send_due_rechecks(db_session, telegram)

    assert result["processed"] == 1
    assert result["failed"] == 1
    assert result["skipped"] == 0

    await db_session.refresh(request1)
    await db_session.refresh(request2)
    # Failed request is retried in 1 hour; processed one is cleared.
    assert request1.recheck_at is not None
    assert request2.recheck_at is None


@pytest.mark.asyncio
async def test_send_due_rechecks_skips_without_quote(
    db_session: AsyncSession,
    seed_employee: Employee,
) -> None:
    telegram = _MockTelegram()
    request = Request(
        group_chat_id=-100123,
        employee_id=seed_employee.id,
        source_text="iPhone",
        normalized_json={"model": "iPhone"},
        status=RequestStatus.needs_recheck,
    )
    db_session.add(request)
    request.recheck_at = datetime.now(UTC) - timedelta(minutes=1)
    await db_session.flush()

    result = await send_due_rechecks(db_session, telegram)

    assert result["processed"] == 0
    assert result["skipped"] == 1


@pytest.mark.asyncio
async def test_schedule_recheck_valid_hours(
    db_session: AsyncSession,
    seed_employee: Employee,
) -> None:
    request = Request(
        group_chat_id=-100123,
        employee_id=seed_employee.id,
        source_text="iPhone",
        normalized_json={"model": "iPhone"},
        status=RequestStatus.priced,
    )
    db_session.add(request)
    await db_session.flush()

    updated = await schedule_recheck(db_session, request_id=request.id, hours=4)
    assert updated.status == RequestStatus.needs_recheck
    assert updated.recheck_at is not None


@pytest.mark.asyncio
async def test_schedule_recheck_invalid_hours(
    db_session: AsyncSession,
) -> None:
    with pytest.raises(InvalidRecheckHoursError):
        await schedule_recheck(db_session, request_id=1, hours=10)
