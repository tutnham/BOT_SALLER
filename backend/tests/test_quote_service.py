"""Unit tests for quote_service (TECH DOC §8.3, §9.2)."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Quote, QuoteSource, Request, RequestStatus, Supplier
from app.services.quote_service import upsert_quote


@pytest.mark.asyncio
async def test_upsert_quote_insert(
    db_session: AsyncSession,
    seed_employee,
    seed_group_chat_id: int,
    seed_suppliers: list[Supplier],
) -> None:
    request = Request(
        group_chat_id=seed_group_chat_id,
        employee_id=seed_employee.id,
        source_text="iPhone",
        status=RequestStatus.awaiting_answers,
    )
    db_session.add(request)
    await db_session.flush()

    supplier = seed_suppliers[0]
    quote = await upsert_quote(
        db_session,
        request.id,
        supplier.id,
        price=85000.0,
        qty=2,
        available=True,
        source=QuoteSource.regex,
        confidence=0.9,
    )
    assert quote is not None
    assert quote.price_initial == 85000
    assert quote.qty == 2
    assert quote.available is True
    assert quote.source == QuoteSource.regex


@pytest.mark.asyncio
async def test_upsert_quote_update_existing(
    db_session: AsyncSession,
    seed_employee,
    seed_group_chat_id: int,
    seed_suppliers: list[Supplier],
) -> None:
    request = Request(
        group_chat_id=seed_group_chat_id,
        employee_id=seed_employee.id,
        source_text="iPhone",
        status=RequestStatus.awaiting_answers,
    )
    db_session.add(request)
    await db_session.flush()

    supplier = seed_suppliers[0]
    await upsert_quote(
        db_session,
        request.id,
        supplier.id,
        price=80000.0,
        source=QuoteSource.regex,
        confidence=0.9,
    )
    quote = await upsert_quote(
        db_session,
        request.id,
        supplier.id,
        price=75000.0,
        source=QuoteSource.manual,
        confidence=1.0,
    )
    assert quote is not None
    assert quote.price_initial == 75000

    count = await db_session.scalar(
        select(Quote.id).where(
            Quote.request_id == request.id,
            Quote.supplier_id == supplier.id,
        )
    )
    assert count is not None


@pytest.mark.asyncio
async def test_low_confidence_skips_write(
    db_session: AsyncSession,
    seed_employee,
    seed_group_chat_id: int,
    seed_suppliers: list[Supplier],
) -> None:
    request = Request(
        group_chat_id=seed_group_chat_id,
        employee_id=seed_employee.id,
        source_text="iPhone",
        status=RequestStatus.awaiting_answers,
    )
    db_session.add(request)
    await db_session.flush()

    supplier = seed_suppliers[0]
    quote = await upsert_quote(
        db_session,
        request.id,
        supplier.id,
        price=85000.0,
        source=QuoteSource.regex,
        confidence=0.5,
    )
    assert quote is None

    existing = await db_session.scalar(
        select(Quote).where(
            Quote.request_id == request.id,
            Quote.supplier_id == supplier.id,
        )
    )
    assert existing is None


@pytest.mark.asyncio
async def test_manual_always_writes(
    db_session: AsyncSession,
    seed_employee,
    seed_group_chat_id: int,
    seed_suppliers: list[Supplier],
) -> None:
    request = Request(
        group_chat_id=seed_group_chat_id,
        employee_id=seed_employee.id,
        source_text="iPhone",
        status=RequestStatus.awaiting_answers,
    )
    db_session.add(request)
    await db_session.flush()

    supplier = seed_suppliers[0]
    quote = await upsert_quote(
        db_session,
        request.id,
        supplier.id,
        price=85000.0,
        source=QuoteSource.manual,
    )
    assert quote is not None
    assert quote.confidence == 1.0


@pytest.mark.asyncio
async def test_first_quote_transitions_to_priced(
    db_session: AsyncSession,
    seed_employee,
    seed_group_chat_id: int,
    seed_suppliers: list[Supplier],
) -> None:
    request = Request(
        group_chat_id=seed_group_chat_id,
        employee_id=seed_employee.id,
        source_text="iPhone",
        status=RequestStatus.awaiting_answers,
    )
    db_session.add(request)
    await db_session.flush()

    supplier = seed_suppliers[0]
    await upsert_quote(
        db_session,
        request.id,
        supplier.id,
        price=85000.0,
        source=QuoteSource.regex,
        confidence=0.9,
    )
    await db_session.refresh(request)
    assert request.status == RequestStatus.priced


@pytest.mark.asyncio
async def test_quote_does_not_change_bargaining_status(
    db_session: AsyncSession,
    seed_employee,
    seed_group_chat_id: int,
    seed_suppliers: list[Supplier],
) -> None:
    request = Request(
        group_chat_id=seed_group_chat_id,
        employee_id=seed_employee.id,
        source_text="iPhone",
        status=RequestStatus.bargaining,
    )
    db_session.add(request)
    await db_session.flush()

    supplier = seed_suppliers[0]
    await upsert_quote(
        db_session,
        request.id,
        supplier.id,
        price=85000.0,
        source=QuoteSource.manual,
        confidence=1.0,
    )
    await db_session.refresh(request)
    assert request.status == RequestStatus.bargaining
