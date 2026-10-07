"""Deterministic best eligible quote selection (no LLM)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import (
    PriceSelectionStatus,
    Quote,
    Request,
    RequestPriceSelection,
)
from app.services.markup_service import apply_markup, load_rules
from app.services.quote_eligibility import is_quote_eligible_for_selection


@dataclass
class BestQuoteSelectionResult:
    request_id: int
    canonical_sku_key: str | None
    selected_quote_id: int | None
    selected_supplier_id: int | None
    purchase_unit_price: Decimal | None
    requested_qty: int | None
    client_unit_price: Decimal | None
    selection_reason: str
    candidate_count: int
    rejected_candidate_reasons: dict[str, str] = field(default_factory=dict)
    selection_version: int = 0
    selected_at: datetime | None = None


async def select_best_quote_for_request(
    session: AsyncSession,
    request_id: int,
    *,
    now: datetime | None = None,
) -> BestQuoteSelectionResult:
    moment = now or datetime.now(UTC)
    request = await session.get(Request, request_id)
    if request is None:
        return BestQuoteSelectionResult(
            request_id=request_id,
            canonical_sku_key=None,
            selected_quote_id=None,
            selected_supplier_id=None,
            purchase_unit_price=None,
            requested_qty=None,
            client_unit_price=None,
            selection_reason="request_not_found",
            candidate_count=0,
        )

    result = await session.execute(
        select(Quote)
        .where(Quote.request_id == request_id)
        .options(selectinload(Quote.supplier))
        .order_by(Quote.id.asc())
    )
    quotes = list(result.scalars().all())
    rejected: dict[str, str] = {}
    eligible: list[tuple[Quote, Decimal, datetime]] = []

    for quote in quotes:
        supplier = quote.supplier
        if supplier is None:
            rejected[str(quote.id)] = "supplier_missing"
            continue
        ok, reason = is_quote_eligible_for_selection(
            quote,
            request,
            supplier,
            now=moment,
            canonical_sku_key=request.canonical_sku_key,
        )
        if not ok:
            rejected[str(quote.id)] = reason
            continue
        price = quote.price_bargain or quote.price_initial
        assert price is not None
        observed = quote.updated_at or quote.created_at
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=UTC)
        eligible.append((quote, Decimal(str(price)), observed))

    if not eligible:
        return BestQuoteSelectionResult(
            request_id=request_id,
            canonical_sku_key=request.canonical_sku_key,
            selected_quote_id=None,
            selected_supplier_id=None,
            purchase_unit_price=None,
            requested_qty=request.requested_qty,
            client_unit_price=None,
            selection_reason="no_eligible_quotes",
            candidate_count=len(quotes),
            rejected_candidate_reasons=rejected,
        )

    eligible.sort(key=lambda row: (row[1], -row[2].timestamp(), row[0].id))
    winner, purchase_price, observed_at = eligible[0]

    rules = await load_rules(session)
    product = request.normalized_json or {"model": request.source_text}
    client_price, markup, _, _ = apply_markup(purchase_price, product, rules)

    qty = request.requested_qty or int((request.normalized_json or {}).get("qty") or 1)
    return BestQuoteSelectionResult(
        request_id=request_id,
        canonical_sku_key=request.canonical_sku_key,
        selected_quote_id=winner.id,
        selected_supplier_id=winner.supplier_id,
        purchase_unit_price=purchase_price,
        requested_qty=qty,
        client_unit_price=client_price,
        selection_reason="lowest_eligible_purchase_price",
        candidate_count=len(quotes),
        rejected_candidate_reasons=rejected,
        selected_at=observed_at,
    )


async def upsert_provisional_selection(
    session: AsyncSession,
    request: Request,
    result: BestQuoteSelectionResult,
) -> RequestPriceSelection | None:
    if result.selected_quote_id is None:
        return None

    existing = await session.scalar(
        select(RequestPriceSelection)
        .where(
            RequestPriceSelection.request_id == request.id,
            RequestPriceSelection.status == PriceSelectionStatus.provisional.value,
        )
        .order_by(RequestPriceSelection.selection_version.desc())
        .limit(1)
    )
    next_version = 1
    if existing is not None:
        next_version = existing.selection_version + 1
        existing.status = PriceSelectionStatus.superseded.value

    row = RequestPriceSelection(
        request_id=request.id,
        batch_id=request.batch_id,
        canonical_sku_key=result.canonical_sku_key,
        status=PriceSelectionStatus.provisional.value,
        selected_quote_id=result.selected_quote_id,
        selected_supplier_id=result.selected_supplier_id,
        purchase_unit_price=result.purchase_unit_price,
        requested_qty=result.requested_qty,
        client_unit_price=result.client_unit_price,
        selection_reason=result.selection_reason,
        candidate_count=result.candidate_count,
        rejected_candidates=result.rejected_candidate_reasons,
        selection_version=next_version,
        selected_at=result.selected_at or datetime.now(UTC),
    )
    session.add(row)
    await session.flush()
    return row


async def recalculate_provisional_for_request(
    session: AsyncSession,
    request_id: int,
) -> BestQuoteSelectionResult:
    result = await select_best_quote_for_request(session, request_id)
    request = await session.get(Request, request_id)
    if request is not None:
        await upsert_provisional_selection(session, request, result)
    return result
