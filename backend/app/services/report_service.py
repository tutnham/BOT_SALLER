"""Owner analytics report service (TECH DOC §9.6)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import (
    Deal,
    MessageIn,
    MessageOut,
    Quote,
    ReportCache,
    Request,
    RequestStatus,
    Supplier,
)
from app.llm.client import LLMProviderError, get_llm_client
from app.templates.report_fallback import render_report_fallback
from app.utils.html import sanitize_telegram_html, sanitize_telegram_report_html

REQUEST_SCOPED_CACHE_TTL = timedelta(minutes=5)


def _to_decimal(value: Decimal | float | int | None) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _build_period_context(
    *,
    period: str,
    request_id: int | None,
    now_msk: datetime,
) -> tuple[str, datetime | None, datetime | None]:
    if request_id is not None:
        return f"request_{request_id}", None, None

    if period == "day":
        start_msk = now_msk.replace(hour=0, minute=0, second=0, microsecond=0)
        end_msk = start_msk + timedelta(days=1)
        return f"{start_msk.date()}_day", start_msk.astimezone(UTC), end_msk.astimezone(
            UTC
        )

    if period == "week":
        start_msk = (
            now_msk - timedelta(days=now_msk.weekday())
        ).replace(hour=0, minute=0, second=0, microsecond=0)
        end_msk = start_msk + timedelta(days=7)
        iso_year, iso_week, _ = start_msk.isocalendar()
        return (
            f"{iso_year}-W{iso_week:02d}_week",
            start_msk.astimezone(UTC),
            end_msk.astimezone(UTC),
        )

    raise ValueError("invalid_period")


def _is_cache_fresh(
    *,
    cache: ReportCache,
    now_utc: datetime,
    period_end_utc: datetime | None,
    request_id: int | None,
) -> bool:
    if request_id is not None:
        return (now_utc - cache.created_at) < REQUEST_SCOPED_CACHE_TTL
    if period_end_utc is None:
        return False
    return now_utc < period_end_utc


def _build_request_filters(
    *,
    request_id: int | None,
    start_utc: datetime | None,
    end_utc: datetime | None,
) -> list[Any]:
    if request_id is not None:
        return [Request.id == request_id]
    if start_utc is None or end_utc is None:
        return []
    return [Request.created_at >= start_utc, Request.created_at < end_utc]


def _json_ready(value: Any) -> Any:
    """Serialize Decimals as strings to keep money precision end-to-end."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, list):
        return [_json_ready(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_ready(item) for key, item in value.items()}
    return value


async def _collect_metrics(
    session: AsyncSession,
    *,
    period: str,
    period_key: str,
    request_filters: list[Any],
) -> dict[str, Any]:
    requests_total = (
        await session.execute(select(func.count(Request.id)).where(*request_filters))
    ).scalar_one()

    deals_closed = (
        await session.execute(
            select(func.count(Deal.id))
            .join(Request, Request.id == Deal.request_id)
            .where(*request_filters)
        )
    ).scalar_one()

    messages_out_total = (
        await session.execute(
            select(func.count(MessageOut.id))
            .join(Request, Request.id == MessageOut.request_id)
            .where(*request_filters)
        )
    ).scalar_one()

    messages_in_total = (
        await session.execute(
            select(func.count(MessageIn.id))
            .join(Request, Request.id == MessageIn.request_id)
            .where(*request_filters)
        )
    ).scalar_one()

    avg_price_initial = (
        await session.execute(
            select(func.avg(Quote.price_initial))
            .join(Request, Request.id == Quote.request_id)
            .where(*request_filters)
        )
    ).scalar_one()

    avg_final_price = (
        await session.execute(
            select(func.avg(Deal.final_price))
            .join(Request, Request.id == Deal.request_id)
            .where(*request_filters)
        )
    ).scalar_one()

    open_requests = (
        await session.execute(
            select(func.count(Request.id)).where(
                *request_filters,
                Request.status.not_in((RequestStatus.closed, RequestStatus.cancelled)),
            )
        )
    ).scalar_one()

    top_rows = (
        await session.execute(
            select(Supplier.name, func.count(Deal.id).label("deals_count"))
            .join(Deal, Deal.chosen_supplier_id == Supplier.id)
            .join(Request, Request.id == Deal.request_id)
            .where(*request_filters)
            .group_by(Supplier.name)
            .order_by(func.count(Deal.id).desc(), Supplier.name.asc())
            .limit(5)
        )
    ).all()
    top_suppliers = [
        {"name": sanitize_telegram_html(supplier_name), "deals_count": int(deals_count)}
        for supplier_name, deals_count in top_rows
    ]

    avg_price_initial_d = _to_decimal(avg_price_initial)
    avg_final_price_d = _to_decimal(avg_final_price)
    savings_vs_initial = None
    if avg_price_initial_d is not None and avg_final_price_d is not None:
        savings_vs_initial = (avg_price_initial_d - avg_final_price_d).quantize(
            Decimal("0.01")
        )

    return {
        "period": period,
        "period_key": period_key,
        "requests_total": int(requests_total or 0),
        "deals_closed": int(deals_closed or 0),
        "messages_out_total": int(messages_out_total or 0),
        "messages_in_total": int(messages_in_total or 0),
        "avg_price_initial": avg_price_initial_d.quantize(Decimal("0.01"))
        if avg_price_initial_d is not None
        else None,
        "avg_final_price": avg_final_price_d.quantize(Decimal("0.01"))
        if avg_final_price_d is not None
        else None,
        "savings_vs_initial": savings_vs_initial,
        "top_suppliers": top_suppliers,
        "open_requests": int(open_requests or 0),
    }


async def build_report(
    session: AsyncSession,
    *,
    period: str,
    request_id: int | None = None,
) -> str:
    settings = get_settings()
    tz = ZoneInfo(settings.tz)
    now_msk = datetime.now(tz)
    now_utc = datetime.now(UTC)

    period_key, start_utc, end_utc = _build_period_context(
        period=period,
        request_id=request_id,
        now_msk=now_msk,
    )
    request_filters = _build_request_filters(
        request_id=request_id,
        start_utc=start_utc,
        end_utc=end_utc,
    )

    cached = await session.scalar(
        select(ReportCache).where(ReportCache.period_key == period_key)
    )
    if (
        cached is not None
        and cached.formatted_text
        and _is_cache_fresh(
            cache=cached,
            now_utc=now_utc,
            period_end_utc=end_utc,
            request_id=request_id,
        )
    ):
        return cached.formatted_text

    metrics = await _collect_metrics(
        session,
        period=period,
        period_key=period_key,
        request_filters=request_filters,
    )

    cache_row = cached
    if cache_row is None:
        cache_row = ReportCache(
            period_key=period_key,
            metrics_json=_json_ready(metrics),
            formatted_text=None,
            created_at=now_utc,
        )
        session.add(cache_row)
    else:
        cache_row.metrics_json = _json_ready(metrics)
        cache_row.formatted_text = None
        cache_row.created_at = now_utc
    await session.flush()

    llm = get_llm_client()
    try:
        formatted = await llm.format_report(metrics)
    except LLMProviderError:
        formatted = render_report_fallback(metrics)
    if not formatted.strip():
        formatted = render_report_fallback(metrics)

    formatted = sanitize_telegram_report_html(formatted)
    if not formatted.strip():
        formatted = sanitize_telegram_report_html(render_report_fallback(metrics))

    cache_row.formatted_text = formatted
    cache_row.created_at = now_utc
    await session.flush()
    return formatted
