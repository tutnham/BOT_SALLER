"""Rebuildable daily best-price projection per canonical SKU."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import (
    DailySkuPrice,
    Quote,
    QuoteSource,
    Request,
    RequestBatch,
    RequestPriceState,
    Supplier,
)
from app.parsers.canonical_sku import NORMALIZER_VERSION
from app.services.best_quote_service import (
    select_best_quote_for_request,
    upsert_provisional_selection,
)
from app.services.markup_service import apply_markup, load_rules
from app.services.quote_eligibility import is_quote_eligible_for_selection
from app.utils.business_time import get_business_day_bounds


def business_date_start(now: datetime | None = None) -> datetime:
    settings = get_settings()
    moment = now or datetime.now(UTC)
    start, _next = get_business_day_bounds(moment, settings.tz)
    return start


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
        existing.observed_at = quote.observed_at or moment
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
        observed_at=quote.observed_at or moment,
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
    if not await _daily_source_quote_eligible(session, row, now=moment):
        return None
    return row


async def _daily_source_quote_eligible(
    session: AsyncSession,
    row: DailySkuPrice,
    *,
    now: datetime,
) -> bool:
    """The projection is reusable only while its source quote stays eligible today."""
    if row.source_quote_id is None:
        return False
    quote = await session.get(Quote, row.source_quote_id)
    if quote is None:
        return False
    request = await session.get(Request, quote.request_id)
    supplier = await session.get(Supplier, quote.supplier_id)
    if request is None or supplier is None:
        return False

    settings = get_settings()
    day_start, next_day_start = get_business_day_bounds(now, settings.tz)
    observed = quote.observed_at or quote.created_at
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=UTC)
    if not (day_start <= observed < next_day_start):
        return False
    if quote.source not in (QuoteSource.manual, QuoteSource.operator_corrected):
        if (quote.confidence if quote.confidence is not None else 1.0) < settings.confidence_threshold:
            return False
    if request.normalizer_version and request.normalizer_version != row.normalizer_version:
        return False

    eligible, _reason = is_quote_eligible_for_selection(
        quote, request, supplier, now=now, canonical_sku_key=row.canonical_sku_key
    )
    return eligible


def _requested_units(request: Request) -> int:
    if request.requested_qty:
        return int(request.requested_qty)
    raw = (request.normalized_json or {}).get("qty")
    if raw:
        return int(raw)
    return 1


async def attach_fresh_daily_quote_to_request(
    session: AsyncSession,
    request: Request,
    *,
    now: datetime | None = None,
) -> Quote | None:
    """Copy today's eligible source quote onto ``request`` and reprice markup for it.

    Returns None when there is no reusable same-day price, or the remembered
    quantity cannot cover this request. Does not publish or send anything.
    """
    if not request.canonical_sku_key:
        return None
    daily = await get_fresh_daily_price(
        session, canonical_sku_key=request.canonical_sku_key, now=now
    )
    if daily is None or daily.source_quote_id is None:
        return None
    source = await session.get(Quote, daily.source_quote_id)
    if source is None:
        return None
    if source.qty is not None and int(source.qty) < _requested_units(request):
        return None
    purchase = source.price_bargain or source.price_initial
    if purchase is None:
        return None

    rules = await load_rules(session)
    product = request.normalized_json or {"model": request.source_text}
    final, markup, rule_id, _rule_key = apply_markup(Decimal(str(purchase)), product, rules)

    existing = await session.scalar(
        select(Quote).where(
            Quote.request_id == request.id,
            Quote.supplier_id == source.supplier_id,
        )
    )
    target = existing or Quote(request_id=request.id, supplier_id=source.supplier_id)
    target.price_initial = source.price_initial
    target.price_bargain = source.price_bargain
    target.qty = source.qty
    target.available = source.available
    target.condition = source.condition
    target.source = source.source
    target.confidence = source.confidence
    target.observed_at = source.observed_at or datetime.now(UTC)
    target.markup_rub = markup
    target.price_final = final
    target.markup_rule_id = rule_id
    if existing is None:
        session.add(target)
    await session.flush()
    return target


async def reuse_today_price_for_request(
    session: AsyncSession,
    request: Request,
    *,
    telegram: Any | None = None,
    now: datetime | None = None,
) -> bool:
    """Deliver today's price into this request's chat. False means the caller must RFQ.

    Success is an outbox row (autopublish on) or a direct client message (flag off).
    A publish error never sticks the request in ``known_today`` without a message.
    """
    if not request.canonical_sku_key:
        return False
    try:
        quote = await attach_fresh_daily_quote_to_request(session, request, now=now)
    except Exception:
        logger.exception("daily quote attach failed request_id={}", request.id)
        return False
    if quote is None:
        return False

    settings = get_settings()
    delivered = False
    try:
        from app.services.first_price_publication_service import (
            publish_first_valid_price,
        )

        delivered = await publish_first_valid_price(
            session, request_id=request.id, trigger="known_today", now=now
        )
    except Exception:
        logger.exception("client price publication failed request_id={}", request.id)
        delivered = False

    if not delivered and not settings.auto_publish_first_valid_price and telegram is not None:
        from app.services.quote_service import display_price_for_group
        from app.templates.messages_ru import render_template

        text = render_template(
            "supplier_quote_parsed",
            request_id=request.id,
            price=display_price_for_group(quote),
        )
        try:
            await telegram.send_message(request.group_chat_id, text)
        except Exception:
            logger.exception("known-today client send failed request_id={}", request.id)
        else:
            delivered = True

    if not delivered:
        if request.price_state in (
            RequestPriceState.known_today.value,
            RequestPriceState.known_today,
        ):
            request.price_state = RequestPriceState.collecting.value
            await session.flush()
        return False

    if request.price_state != RequestPriceState.published.value:
        request.price_state = RequestPriceState.known_today.value
    from app.services.telemetry import record_same_day_price_reuse

    record_same_day_price_reuse()
    await session.flush()
    return True


async def apply_known_daily_prices_for_batch(
    session: AsyncSession,
    *,
    batch: RequestBatch,
    telegram: Any | None = None,
) -> int:
    moment = datetime.now(UTC)
    applied = 0
    for request in batch.requests:
        if not request.canonical_sku_key:
            continue
        if await reuse_today_price_for_request(
            session, request, telegram=telegram, now=moment
        ):
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
