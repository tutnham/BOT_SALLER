"""Supplier price correction and duplicate detection."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import MessageIn, Quote, QuotePriceEvent
from app.parsers.product_normalizer import extract_product_attrs, normalize_product_text


def _normalized_product_key(text: str) -> str:
    attrs = extract_product_attrs(text)
    parts = [
        attrs.number or "",
        attrs.variant or "",
        attrs.storage or "",
        attrs.color or "",
        attrs.sim or "",
    ]
    return normalize_product_text(" ".join(parts))


async def classify_supplier_followup(
    session: AsyncSession,
    *,
    supplier_id: int,
    request_id: int,
    new_price: Decimal | None,
    new_text: str,
    message_in_id: int,
) -> str:
    """Returns new | duplicate | correction."""
    if new_price is None:
        return "new"
    settings = get_settings()
    window = settings.supplier_correction_window_minutes
    if window is None:
        return "new"

    since = datetime.now(timezone.utc) - timedelta(minutes=window)
    prior = await session.scalar(
        select(MessageIn)
        .where(
            MessageIn.supplier_id == supplier_id,
            MessageIn.request_id == request_id,
            MessageIn.id != message_in_id,
            MessageIn.received_at >= since,
        )
        .order_by(MessageIn.received_at.desc())
        .limit(1)
    )
    if prior is None:
        return "new"
    if _normalized_product_key(prior.raw_text) != _normalized_product_key(new_text):
        return "new"

    quote = await session.scalar(
        select(Quote).where(
            Quote.request_id == request_id,
            Quote.supplier_id == supplier_id,
        )
    )
    if quote is None or quote.price_initial is None:
        return "new"
    if quote.price_initial == new_price:
        return "duplicate"
    return "correction"


async def record_quote_correction(
    session: AsyncSession,
    *,
    quote_id: int,
    old_price: Decimal | None,
    new_price: Decimal | None,
    message_in_id: int,
    reason: str = "correction",
) -> None:
    session.add(
        QuotePriceEvent(
            quote_id=quote_id,
            old_price=old_price,
            new_price=new_price,
            message_in_id=message_in_id,
            reason=reason,
        )
    )
    await session.flush()
