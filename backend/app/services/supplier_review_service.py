"""Employee inbox for unresolved supplier RFQ replies."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.orm import joinedload

from app.db.models import (
    Employee,
    MessageIn,
    Request,
    Supplier,
    SupplierMessageItem,
)
from app.parsers.price_parse import parse_price_from_text
from app.parsers.product_normalizer import extract_product_attrs


async def ensure_review_item_for_message(
    session: AsyncSession,
    *,
    message_in: MessageIn,
    line_no: int = 1,
    raw_line: str | None = None,
    conflict_reason: str | None = None,
    candidate_request_ids: list[int] | None = None,
) -> SupplierMessageItem:
    existing = await session.scalar(
        select(SupplierMessageItem).where(
            SupplierMessageItem.message_in_id == message_in.id,
            SupplierMessageItem.line_no == line_no,
        )
    )
    if existing is not None:
        return existing

    text = (raw_line or message_in.raw_text).strip()
    price_result = parse_price_from_text(text)
    attrs = extract_product_attrs(text)
    parsed_json: dict[str, Any] = {
        "attrs": attrs.as_normalized_json(price_result.confidence),
        "price_audit": price_result.as_audit_dict(),
        "candidate_request_ids": candidate_request_ids or [],
    }
    item = SupplierMessageItem(
        message_in_id=message_in.id,
        line_no=line_no,
        raw_text=text,
        parsed_json=parsed_json,
        parsed_price=price_result.price,
        parse_method=price_result.method,
        bind_status="pending",
        conflict_reason=conflict_reason,
        confidence=price_result.confidence,
    )
    session.add(item)
    await session.flush()
    return item


async def list_pending_items_for_employee(
    session: AsyncSession,
    *,
    employee: Employee,
    limit: int = 20,
) -> list[SupplierMessageItem]:
    my_request_ids = set(
        (
            await session.execute(
                select(Request.id).where(Request.employee_id == employee.id)
            )
        )
        .scalars()
        .all()
    )
    items = await list_all_pending_items(session, limit=100)
    filtered: list[SupplierMessageItem] = []
    for item in items:
        if item.request_id is not None and item.request_id in my_request_ids:
            filtered.append(item)
            continue
        candidates = (item.parsed_json or {}).get("candidate_request_ids") or []
        if any(int(cid) in my_request_ids for cid in candidates):
            filtered.append(item)
    return filtered[:limit]


async def list_all_pending_items(
    session: AsyncSession,
    *,
    limit: int = 30,
) -> list[SupplierMessageItem]:
    stmt = (
        select(SupplierMessageItem)
        .where(SupplierMessageItem.bind_status == "pending")
        .options(
            selectinload(SupplierMessageItem.message_in).selectinload(MessageIn.supplier),
        )
        .order_by(SupplierMessageItem.created_at.asc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def lock_pending_item(
    session: AsyncSession,
    item_id: int,
) -> SupplierMessageItem | None:
    stmt = (
        select(SupplierMessageItem)
        .where(
            SupplierMessageItem.id == item_id,
            SupplierMessageItem.bind_status == "pending",
        )
        .options(joinedload(SupplierMessageItem.message_in))
        .with_for_update()
    )
    return await session.scalar(stmt)


async def resolve_item(
    session: AsyncSession,
    item: SupplierMessageItem,
    *,
    request_id: int | None,
    bind_method: str,
) -> None:
    item.request_id = request_id
    item.bind_status = "resolved"
    item.bind_method = bind_method
    item.resolved_at = datetime.now(timezone.utc)
    await session.flush()


async def employee_can_resolve_item(
    session: AsyncSession,
    *,
    employee: Employee,
    item: SupplierMessageItem,
    request_id: int,
) -> bool:
    request = await session.get(Request, request_id)
    if request is None:
        return False
    return request.employee_id == employee.id
