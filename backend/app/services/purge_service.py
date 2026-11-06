"""Hard-delete requests and related rows (owner-only purge)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    DailySkuPrice,
    Deal,
    MarkupRule,
    MessageIn,
    MessageOut,
    Quote,
    Request,
    RequestBatch,
    RequestPriceSelection,
    RequestStatus,
    Supplier,
    SupplierBindPrompt,
    SupplierRfqBatch,
    SupplierRfqBatchItem,
    TelegramOutbox,
)

DEFAULT_PURGE_OLD_LIMIT = 50


async def _delete_request_children(
    session: AsyncSession,
    request_ids: list[int],
) -> None:
    """Delete child rows for one or more request ids in FK-safe order."""
    if not request_ids:
        return
    await session.execute(
        delete(MessageOut).where(MessageOut.request_id.in_(request_ids))
    )
    await session.execute(
        delete(MessageIn).where(MessageIn.request_id.in_(request_ids))
    )
    await session.execute(delete(Quote).where(Quote.request_id.in_(request_ids)))
    await session.execute(delete(Deal).where(Deal.request_id.in_(request_ids)))


async def hard_delete_request(
    session: AsyncSession,
    request_id: int,
    *,
    actor_telegram_id: int | None = None,
) -> bool:
    """
    Hard-delete request and all dependent rows in one transaction.

    Returns:
        True if request existed and was deleted, False if not found.
    """
    request = await session.get(Request, request_id)
    if request is None:
        return False

    from app.services.audit_service import record_admin_mutation

    await _delete_request_children(session, [request_id])
    await session.execute(delete(Request).where(Request.id == request_id))
    await record_admin_mutation(
        session,
        action="request_purge",
        entity_type="request",
        entity_id=str(request_id),
        actor_telegram_id=actor_telegram_id,
        previous_state={"status": str(request.status)},
        new_state={"deleted": True},
    )
    await session.flush()
    return True


async def purge_old_requests(
    session: AsyncSession,
    *,
    days: int,
    limit: int = DEFAULT_PURGE_OLD_LIMIT,
    dry_run: bool = True,
) -> tuple[int, list[int]]:
    """
    Find or delete cancelled/closed requests older than ``days``.

    Returns:
        (count, list of request ids matched or deleted)
    """
    cutoff = datetime.now(UTC) - timedelta(days=days)
    result = await session.execute(
        select(Request.id)
        .where(
            Request.status.in_((RequestStatus.cancelled, RequestStatus.closed)),
            Request.created_at < cutoff,
        )
        .order_by(Request.created_at.asc())
        .limit(limit)
    )
    request_ids = [int(row) for row in result.scalars().all()]
    if dry_run or not request_ids:
        return len(request_ids), request_ids

    await _delete_request_children(session, request_ids)
    await session.execute(delete(Request).where(Request.id.in_(request_ids)))
    await session.flush()
    return len(request_ids), request_ids


async def count_request_ops(session: AsyncSession) -> dict[str, int]:
    counts: dict[str, int] = {}
    for name, model in (
        ("requests", Request),
        ("request_batches", RequestBatch),
        ("quotes", Quote),
        ("deals", Deal),
        ("daily_sku_prices", DailySkuPrice),
        ("request_price_selections", RequestPriceSelection),
        ("messages_out", MessageOut),
        ("messages_in", MessageIn),
        ("supplier_bind_prompts", SupplierBindPrompt),
        ("supplier_rfq_batches", SupplierRfqBatch),
        ("telegram_outbox", TelegramOutbox),
        ("markup_rules", MarkupRule),
        ("suppliers", Supplier),
    ):
        counts[name] = int(await session.scalar(select(func.count()).select_from(model)) or 0)
    return counts


async def purge_all_request_data(session: AsyncSession) -> dict[str, dict[str, int]]:
    """Delete every request and its operational trail. Directories stay."""
    before = await count_request_ops(session)
    await session.execute(delete(SupplierBindPrompt))
    await session.execute(delete(Deal))
    await session.execute(delete(RequestPriceSelection))
    await session.execute(delete(SupplierRfqBatchItem))
    await session.execute(delete(SupplierRfqBatch))
    await session.execute(delete(DailySkuPrice))
    await session.execute(delete(Quote))
    await session.execute(delete(TelegramOutbox))
    await session.execute(delete(MessageOut))
    await session.execute(delete(MessageIn))
    await session.execute(delete(Request))
    await session.execute(delete(RequestBatch))
    await session.flush()
    after = await count_request_ops(session)
    return {"before": before, "after": after}
