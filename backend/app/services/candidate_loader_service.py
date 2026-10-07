"""Route-aware eligible request candidates for supplier inbound binding."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import (
    MessageKind,
    MessageOut,
    MessageSendStatus,
    Quote,
    Request,
    RequestStatus,
    RfqItemDeliveryStatus,
    Supplier,
    SupplierCategory,
    SupplierRfqBatchItem,
)
from app.services.product_classifier import (
    category_from_normalized,
    classify_product_deterministic,
)
from app.services.route_identity import routes_match

_ACTIVE_STATUSES = (
    RequestStatus.awaiting_answers,
    RequestStatus.bargaining,
    RequestStatus.needs_recheck,
    RequestStatus.priced,
    RequestStatus.open,
)


@dataclass(frozen=True)
class EligibleCandidate:
    request: Request
    latest_kind: MessageKind
    latest_sent_at: datetime


def _request_category(request: Request) -> str | None:
    category = category_from_normalized(request.normalized_json)
    if category:
        return category
    return classify_product_deterministic(request.source_text)


async def load_eligible_candidates(
    session: AsyncSession,
    *,
    supplier: Supplier,
    chat_id: int,
    business_connection_id: str | None,
    received_at: datetime | None = None,
    expected_purpose: MessageKind | None = None,
    include_quoted_ask_for_correction: bool = False,
) -> list[Request]:
    """Return open requests eligible for binding on this supplier route."""
    settings = get_settings()
    now = received_at or datetime.now(UTC)
    max_age = settings.supplier_reply_max_age_hours
    cutoff = now - timedelta(hours=max_age)

    stmt = (
        select(MessageOut, Request)
        .join(Request, Request.id == MessageOut.request_id)
        .where(
            MessageOut.supplier_id == supplier.id,
            MessageOut.chat_id == chat_id,
            MessageOut.send_status == MessageSendStatus.sent.value,
            MessageOut.request_id.isnot(None),
            MessageOut.sent_at >= cutoff,
            Request.status.in_(_ACTIVE_STATUSES),
        )
        .order_by(MessageOut.request_id, MessageOut.sent_at.desc())
    )
    rows = (await session.execute(stmt)).all()

    latest_by_request: dict[int, EligibleCandidate] = {}
    for outbound, request in rows:
        if not routes_match(outbound.business_connection_id, business_connection_id):
            continue
        if request.id in latest_by_request:
            continue
        latest_by_request[request.id] = EligibleCandidate(
            request=request,
            latest_kind=outbound.kind,
            latest_sent_at=outbound.sent_at,
        )

    batch_stmt = (
        select(MessageOut, Request)
        .join(
            SupplierRfqBatchItem,
            SupplierRfqBatchItem.message_out_id == MessageOut.id,
        )
        .join(Request, Request.id == SupplierRfqBatchItem.request_id)
        .where(
            MessageOut.supplier_id == supplier.id,
            MessageOut.chat_id == chat_id,
            MessageOut.send_status == MessageSendStatus.sent.value,
            MessageOut.sent_at >= cutoff,
            SupplierRfqBatchItem.delivery_status == RfqItemDeliveryStatus.sent.value,
            Request.status.in_(_ACTIVE_STATUSES),
        )
        .order_by(Request.id, MessageOut.sent_at.desc())
    )
    for outbound, request in (await session.execute(batch_stmt)).all():
        if not routes_match(outbound.business_connection_id, business_connection_id):
            continue
        if request.id in latest_by_request:
            continue
        latest_by_request[request.id] = EligibleCandidate(
            request=request,
            latest_kind=outbound.kind,
            latest_sent_at=outbound.sent_at,
        )

    if not latest_by_request:
        return []

    request_ids = list(latest_by_request.keys())
    quoted_ids = set(
        (
            await session.execute(
                select(Quote.request_id).where(
                    Quote.supplier_id == supplier.id,
                    Quote.request_id.in_(request_ids),
                    Quote.price_initial.is_not(None),
                )
            )
        ).scalars().all()
    )

    filtered: list[Request] = []
    for candidate in latest_by_request.values():
        kind = candidate.latest_kind
        if expected_purpose is not None and kind != expected_purpose:
            continue
        if (
            kind is MessageKind.ask
            and candidate.request.id in quoted_ids
            and not include_quoted_ask_for_correction
        ):
            continue
        if kind is MessageKind.bargain and candidate.request.status is not RequestStatus.bargaining:
            continue
        if kind is MessageKind.recheck and candidate.request.status is not RequestStatus.needs_recheck:
            continue
        filtered.append(candidate.request)

    cat_result = await session.execute(
        select(SupplierCategory.category).where(SupplierCategory.supplier_id == supplier.id)
    )
    supplier_categories = set(cat_result.scalars().all())
    if not supplier_categories:
        return []

    result: list[Request] = []
    for request in filtered:
        category = _request_category(request)
        if category is None or category == "unknown":
            continue
        if category in supplier_categories:
            result.append(request)
    result.sort(key=lambda item: item.id, reverse=True)
    return result


async def load_open_requests_for_supplier(
    session: AsyncSession,
    *,
    supplier: Supplier,
    chat_id: int | None = None,
    business_connection_id: str | None = None,
) -> list[Request]:
    """Backward-compatible wrapper when route is unknown (returns empty)."""
    if chat_id is None:
        return []
    return await load_eligible_candidates(
        session,
        supplier=supplier,
        chat_id=chat_id,
        business_connection_id=business_connection_id,
    )
