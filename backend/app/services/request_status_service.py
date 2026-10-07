"""Per-request RFQ counters for employee status cards."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MessageIn, MessageOut, Quote, Request, SupplierMessageItem


@dataclass(frozen=True)
class RequestRfqCounters:
    sent_count: int
    delivered_count: int
    replied_count: int
    quoted_count: int
    pending_review_count: int
    failed_count: int


async def load_request_rfq_counters(
    session: AsyncSession,
    request_id: int,
) -> RequestRfqCounters:
    sent_count = int(
        await session.scalar(
            select(func.count()).select_from(MessageOut).where(MessageOut.request_id == request_id)
        )
        or 0
    )
    delivered_count = int(
        await session.scalar(
            select(func.count())
            .select_from(MessageOut)
            .where(
                MessageOut.request_id == request_id,
                MessageOut.send_status == "sent",
            )
        )
        or 0
    )
    replied_count = int(
        await session.scalar(
            select(func.count())
            .select_from(MessageIn)
            .where(
                MessageIn.request_id == request_id,
                MessageIn.bind_status.in_(("bound", "pending_binding")),
            )
        )
        or 0
    )
    quoted_count = int(
        await session.scalar(
            select(func.count())
            .select_from(Quote)
            .where(Quote.request_id == request_id, Quote.price_initial.is_not(None))
        )
        or 0
    )
    pending_review_count = int(
        await session.scalar(
            select(func.count())
            .select_from(SupplierMessageItem)
            .join(MessageIn, MessageIn.id == SupplierMessageItem.message_in_id)
            .where(
                SupplierMessageItem.bind_status == "pending",
                SupplierMessageItem.request_id == request_id,
            )
        )
        or 0
    )
    failed_count = int(
        await session.scalar(
            select(func.count())
            .select_from(MessageOut)
            .where(
                MessageOut.request_id == request_id,
                MessageOut.send_status == "failed",
            )
        )
        or 0
    )
    return RequestRfqCounters(
        sent_count=sent_count,
        delivered_count=delivered_count,
        replied_count=replied_count,
        quoted_count=quoted_count,
        pending_review_count=pending_review_count,
        failed_count=failed_count,
    )
