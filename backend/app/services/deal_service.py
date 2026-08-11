"""Deal creation and request cancellation (TECH DOC §9.3.1, §11)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Deal, DealOutcome, Request, RequestStatus, Supplier


class RequestNotFoundError(Exception):
    """Request id does not exist."""


class SupplierNotFoundError(Exception):
    """Supplier id does not exist."""


class RequestNotOpenError(Exception):
    """Request is closed or cancelled — operation not allowed."""


class DealAlreadyExistsError(Exception):
    """Deal already recorded for this request."""


async def create_deal(
    session: AsyncSession,
    *,
    request_id: int,
    supplier_id: int,
    final_price: Decimal,
) -> Deal:
    """
    Record won deal and close request.

    Raises:
        RequestNotFoundError, SupplierNotFoundError, RequestNotOpenError,
        DealAlreadyExistsError
    """
    request = await session.get(Request, request_id)
    if request is None:
        raise RequestNotFoundError(request_id)

    supplier = await session.get(Supplier, supplier_id)
    if supplier is None:
        raise SupplierNotFoundError(supplier_id)

    if request.status in (RequestStatus.closed, RequestStatus.cancelled):
        raise RequestNotOpenError(request_id)

    existing = await session.execute(
        select(Deal.id).where(Deal.request_id == request_id).limit(1)
    )
    if existing.scalar_one_or_none() is not None:
        raise DealAlreadyExistsError(request_id)

    deal = Deal(
        request_id=request_id,
        chosen_supplier_id=supplier_id,
        final_price=final_price,
        outcome=DealOutcome.won,
        closed_at=datetime.now(UTC),
    )
    session.add(deal)
    request.status = RequestStatus.closed
    await session.flush()
    return deal


async def cancel_request(session: AsyncSession, *, request_id: int) -> Request:
    """
    Cancel request.

    Raises:
        RequestNotFoundError, RequestNotOpenError
    """
    request = await session.get(Request, request_id)
    if request is None:
        raise RequestNotFoundError(request_id)

    if request.status in (RequestStatus.closed, RequestStatus.cancelled):
        raise RequestNotOpenError(request_id)

    request.status = RequestStatus.cancelled
    await session.flush()
    return request
