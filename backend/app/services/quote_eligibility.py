"""Deterministic rules for quote participation in best-price selection."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.config import get_settings
from app.db.models import Quote, QuoteSource, Request, RequestStatus, Supplier


def _purchase_unit_price(quote: Quote) -> Decimal | None:
    if quote.price_bargain is not None:
        return Decimal(str(quote.price_bargain))
    if quote.price_initial is not None:
        return Decimal(str(quote.price_initial))
    return None


def is_quote_eligible_for_selection(
    quote: Quote,
    request: Request,
    supplier: Supplier,
    *,
    now: datetime | None = None,
    canonical_sku_key: str | None = None,
) -> tuple[bool, str]:
    """Return (eligible, rejection_reason). Empty reason when eligible."""
    settings = get_settings()
    moment = now or datetime.now(UTC)

    if request.status in (RequestStatus.closed, RequestStatus.cancelled):
        return False, "request_closed"
    if not supplier.active:
        return False, "supplier_inactive"
    if quote.available is False:
        return False, "not_available"
    conf = quote.confidence if quote.confidence is not None else 1.0
    if quote.source not in (QuoteSource.manual, QuoteSource.operator_corrected):
        if conf < settings.confidence_threshold:
            return False, "low_confidence"

    price = _purchase_unit_price(quote)
    if price is None or price <= 0:
        return False, "no_price"
    if price < settings.supplier_price_min_rub or price > settings.supplier_price_max_rub:
        return False, "price_out_of_sanity"

    age_cutoff = moment - timedelta(hours=settings.supplier_reply_max_age_hours)
    observed = quote.updated_at or quote.created_at
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=UTC)
    if observed < age_cutoff:
        return False, "stale_quote"

    requested_qty = request.requested_qty or (request.normalized_json or {}).get("qty") or 1
    if quote.qty is not None and quote.qty < int(requested_qty):
        return False, "insufficient_qty"

    if canonical_sku_key and request.canonical_sku_key:
        if request.canonical_sku_key != canonical_sku_key:
            return False, "sku_mismatch"

    return True, ""
