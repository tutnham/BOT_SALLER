"""Rebuildable daily best-price projection per canonical SKU."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import DailySkuPrice, Quote, Request, RequestBatch, RequestPriceState
from app.parsers.canonical_sku import NORMALIZER_VERSION
from app.services.best_quote_service import (
    select_best_quote_for_request,
    upsert_provisional_selection,
)
from app.services.markup_service import apply_markup, load_rules


def business_date_start(now: datetime | None = None) -> datetime:
    settings = get_settings()
    tz = ZoneInfo(settings.tz)
    moment = now or datetime.now(UTC)
    local = moment.astimezone(tz)
    start_local = datetime.combine(local.date(), time.min, tzinfo=tz)
    return start_local.astimezone(UTC)


def business_date_only(now: datetime | None = None) -> date:
    settings = get_settings()
    tz = ZoneInfo(settings.tz)
    moment = now or datetime.now(UTC)
    return moment.astimezone(tz).date()


async def upsert_daily_from_quote(
    session: AsyncSession,
    *,
    quote: Quote,
    request: Request,
    now: datetime | None = None,
) -> DailySkuPrice | None:
    if not request.canonical_sku_key:
        return None
    moment = now or datetime.now(UTC)
    settings = get_settings()
    purchase = quote.price_bargain or quote.price_initial
    if purchase is None:
        return None
    rules = await load_rules(session)
    product = request.normalized_json or {"model": request.source_text}
    client_price, _, _, _ = apply_markup(Decimal(str(purchase)), product, rules)
    biz_date = business_date_start(moment)
    expires = moment + timedelta(minutes=settings.daily_price_max_age_minutes)

    purchase_dec = Decimal(str(purchase))
    existing = await session.scalar(
        select(DailySkuPrice).where(
            DailySkuPrice.business_date == biz_date,
            DailySkuPrice.canonical_sku_key == request.canonical_sku_key,
        )
    )
    if existing is not None and existing.purchase_unit_price <= purchase_dec:
        return existing
    if existing is not None:
        existing.source_quote_id = quote.id
        existing.supplier_id = quote.supplier_id
        existing.purchase_unit_price = purchase_dec
        existing.client_unit_price = client_price
        existing.available = quote.available
        existing.available_qty = quote.qty
        existing.observed_at = moment
        existing.expires_at = expires
        existing.invalidated_at = None
        existing.selection_version = existing.selection_version + 1
        await session.flush()
        return existing

    row = DailySkuPrice(
        business_date=biz_date,
        canonical_sku_key=request.canonical_sku_key,
        source_quote_id=quote.id,
        supplier_id=quote.supplier_id,
        purchase_unit_price=purchase_dec,
        client_unit_price=client_price,
        available=quote.available,
        available_qty=quote.qty,
        observed_at=moment,
        expires_at=expires,
        normalizer_version=NORMALIZER_VERSION,
    )
    session.add(row)
    await session.flush()
    return row


async def get_fresh_daily_price(
    session: AsyncSession,
    *,
    canonical_sku_key: str,
    now: datetime | None = None,
) -> DailySkuPrice | None:
    settings = get_settings()
    if not settings.daily_price_reuse_enabled:
        return None
    moment = now or datetime.now(UTC)
    biz_date = business_date_start(moment)
    row = await session.scalar(
        select(DailySkuPrice).where(
            DailySkuPrice.business_date == biz_date,
            DailySkuPrice.canonical_sku_key == canonical_sku_key,
            DailySkuPrice.invalidated_at.is_(None),
        )
    )
    if row is None:
        return None
    if row.expires_at is not None and row.expires_at < moment:
        return None
    return row


async def apply_known_daily_prices_for_batch(
    session: AsyncSession,
    *,
    batch: RequestBatch,
) -> int:
    moment = datetime.now(UTC)
    applied = 0
    for request in batch.requests:
        if not request.canonical_sku_key:
            continue
        daily = await get_fresh_daily_price(
            session, canonical_sku_key=request.canonical_sku_key, now=moment
        )
        if daily is None:
            continue
        request.price_state = RequestPriceState.known_today
        applied += 1
    batch.items_priced = applied
    await session.flush()
    return applied


async def on_quote_upserted(
    session: AsyncSession,
    *,
    quote: Quote,
    request: Request,
) -> None:
    await upsert_daily_from_quote(session, quote=quote, request=request)
    if request.batch_id is not None and request.price_state != RequestPriceState.published:
        result = await select_best_quote_for_request(session, request.id)
        await upsert_provisional_selection(session, request, result)
