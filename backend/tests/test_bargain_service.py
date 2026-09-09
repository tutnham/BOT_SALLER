"""Direct tests for bargain service."""

from __future__ import annotations

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
    SupplierChat,
    SupplierChatType,
)
from app.services.bargain_service import (
    BargainStatusError,
    NoEligibleQuoteError,
    SupplierUnavailableError,
    start_bargain,
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


async def _priced_request(
    db_session: AsyncSession,
    seed_suppliers: list[Supplier],
    seed_employee: Employee,
    supplier_index: int = 0,
) -> tuple[Request, Supplier]:
    supplier = seed_suppliers[supplier_index]
    request = Request(
        group_chat_id=-100123,
        employee_id=seed_employee.id,
        source_text="iPhone 17 256GB",
        normalized_json={"model": "iPhone 17", "storage": "256GB"},
        status=RequestStatus.priced,
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
    db_session.add(
        SupplierChat(
            supplier_id=supplier.id,
            chat_id=supplier.telegram_id,
            chat_type=SupplierChatType.private,
            active=True,
            is_default=True,
        )
    )
    await db_session.flush()
    return request, supplier


@pytest.mark.asyncio
async def test_start_bargain_happy_path(
    db_session: AsyncSession,
    seed_suppliers: list[Supplier],
    seed_employee: Employee,
) -> None:
    request, supplier = await _priced_request(db_session, seed_suppliers, seed_employee, 0)
    telegram = _MockTelegram()

    outbound = await start_bargain(
        db_session,
        request_id=request.id,
        target_price=Decimal("80000"),
        telegram=telegram,
    )

    await db_session.refresh(request)
    assert request.status == RequestStatus.bargaining
    assert outbound.kind == MessageKind.bargain
    assert outbound.supplier_id == supplier.id
    assert telegram.sent == [(supplier.telegram_id, outbound.text)]
    assert "80000" in outbound.text


@pytest.mark.asyncio
async def test_start_bargain_with_explicit_supplier(
    db_session: AsyncSession,
    seed_suppliers: list[Supplier],
    seed_employee: Employee,
) -> None:
    request, _ = await _priced_request(db_session, seed_suppliers, seed_employee, 0)
    chosen = seed_suppliers[1]
    db_session.add(
        SupplierChat(
            supplier_id=chosen.id,
            chat_id=chosen.telegram_id,
            chat_type=SupplierChatType.private,
            active=True,
            is_default=True,
        )
    )
    db_session.add(
        Quote(
            request_id=request.id,
            supplier_id=chosen.id,
            price_initial=Decimal("90000"),
            source=QuoteSource.manual,
            confidence=1.0,
        )
    )
    await db_session.flush()
    telegram = _MockTelegram()

    outbound = await start_bargain(
        db_session,
        request_id=request.id,
        target_price=Decimal("80000"),
        telegram=telegram,
        supplier_id=chosen.id,
    )

    assert outbound.supplier_id == chosen.id


@pytest.mark.asyncio
async def test_start_bargain_closed_request_rejected(
    db_session: AsyncSession,
    seed_suppliers: list[Supplier],
    seed_employee: Employee,
) -> None:
    request, _ = await _priced_request(db_session, seed_suppliers, seed_employee, 0)
    request.status = RequestStatus.closed
    telegram = _MockTelegram()

    with pytest.raises(BargainStatusError):
        await start_bargain(
            db_session,
            request_id=request.id,
            target_price=Decimal("80000"),
            telegram=telegram,
        )


@pytest.mark.asyncio
async def test_start_bargain_no_quotes(
    db_session: AsyncSession,
    seed_suppliers: list[Supplier],
    seed_employee: Employee,
) -> None:
    seed_suppliers[0]
    request = Request(
        group_chat_id=-100123,
        employee_id=seed_employee.id,
        source_text="iPhone",
        normalized_json={"model": "iPhone"},
        status=RequestStatus.awaiting_answers,
    )
    db_session.add(request)
    await db_session.flush()
    telegram = _MockTelegram()

    with pytest.raises(NoEligibleQuoteError):
        await start_bargain(
            db_session,
            request_id=request.id,
            target_price=Decimal("80000"),
            telegram=telegram,
        )


@pytest.mark.asyncio
async def test_start_bargain_telegram_failure_is_unavailable(
    db_session: AsyncSession,
    seed_suppliers: list[Supplier],
    seed_employee: Employee,
) -> None:
    request, supplier = await _priced_request(db_session, seed_suppliers, seed_employee, 0)
    telegram = _MockTelegram(fail_for={supplier.telegram_id})

    with pytest.raises(SupplierUnavailableError):
        await start_bargain(
            db_session,
            request_id=request.id,
            target_price=Decimal("80000"),
            telegram=telegram,
        )

    await db_session.refresh(request)
    # Status is committed before send; failure leaves it as bargaining.
    assert request.status == RequestStatus.bargaining
    outs = (
        await db_session.execute(
            select(MessageOut).where(
                MessageOut.request_id == request.id,
                MessageOut.kind == MessageKind.bargain,
            )
        )
    ).scalars().all()
    assert outs == []
