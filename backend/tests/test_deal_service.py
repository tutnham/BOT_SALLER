"""Direct tests for deal lifecycle helpers."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Employee, Request, RequestStatus, Supplier
from app.services.deal_service import (
    RequestNotFoundError,
    RequestNotOpenError,
    cancel_request,
    create_deal,
)


@pytest.mark.asyncio
async def test_create_deal_closes_request(
    db_session: AsyncSession,
    seed_suppliers: list[Supplier],
    seed_employee: Employee,
) -> None:
    request = Request(
        group_chat_id=-100123,
        employee_id=seed_employee.id,
        source_text="iPhone 17 256GB",
        normalized_json={"model": "iPhone 17"},
        status=RequestStatus.priced,
    )
    db_session.add(request)
    await db_session.flush()
    supplier = seed_suppliers[0]

    deal = await create_deal(
        db_session,
        request_id=request.id,
        supplier_id=supplier.id,
        final_price=Decimal("85000"),
    )

    assert deal.request_id == request.id
    assert deal.chosen_supplier_id == supplier.id
    assert deal.final_price == Decimal("85000")
    await db_session.refresh(request)
    assert request.status == RequestStatus.closed


@pytest.mark.asyncio
async def test_create_deal_request_not_found(
    db_session: AsyncSession,
    seed_suppliers: list[Supplier],
) -> None:
    with pytest.raises(RequestNotFoundError):
        await create_deal(
            db_session,
            request_id=999999,
            supplier_id=seed_suppliers[0].id,
            final_price=Decimal("100"),
        )


@pytest.mark.asyncio
async def test_create_deal_request_closed_rejected(
    db_session: AsyncSession,
    seed_suppliers: list[Supplier],
    seed_employee: Employee,
) -> None:
    request = Request(
        group_chat_id=-100123,
        employee_id=seed_employee.id,
        source_text="iPhone",
        normalized_json={"model": "iPhone"},
        status=RequestStatus.closed,
    )
    db_session.add(request)
    await db_session.flush()

    with pytest.raises(RequestNotOpenError):
        await create_deal(
            db_session,
            request_id=request.id,
            supplier_id=seed_suppliers[0].id,
            final_price=Decimal("100"),
        )


@pytest.mark.asyncio
async def test_create_deal_already_exists(
    db_session: AsyncSession,
    seed_suppliers: list[Supplier],
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

    await create_deal(
        db_session,
        request_id=request.id,
        supplier_id=seed_suppliers[0].id,
        final_price=Decimal("100"),
    )

    with pytest.raises(RequestNotOpenError):
        await create_deal(
            db_session,
            request_id=request.id,
            supplier_id=seed_suppliers[0].id,
            final_price=Decimal("100"),
        )


@pytest.mark.asyncio
async def test_cancel_request(
    db_session: AsyncSession,
    seed_employee: Employee,
) -> None:
    request = Request(
        group_chat_id=-100123,
        employee_id=seed_employee.id,
        source_text="iPhone",
        normalized_json={"model": "iPhone"},
        status=RequestStatus.open,
    )
    db_session.add(request)
    await db_session.flush()

    cancelled = await cancel_request(db_session, request_id=request.id)
    assert cancelled.status == RequestStatus.cancelled
