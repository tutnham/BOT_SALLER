"""Procurement reports based on actual deals."""

from __future__ import annotations

import csv
import io
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.db.models import Deal, DealOutcome, Request
from app.templates.messages_ru import render_template


def _csv_cell(value: str) -> str:
    if value and value[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def _day_bounds(day: date, tz_name: str) -> tuple[datetime, datetime]:
    tz = ZoneInfo(tz_name)
    start = datetime.combine(day, time.min, tzinfo=tz).astimezone(UTC)
    end = start + timedelta(days=1)
    return start, end


async def _load_deals_in_range(
    session: AsyncSession,
    *,
    start: datetime,
    end: datetime,
    batch_id: int | None = None,
) -> list[Deal]:
    stmt = (
        select(Deal)
        .where(
            Deal.closed_at.isnot(None),
            Deal.closed_at >= start,
            Deal.closed_at < end,
            Deal.outcome == DealOutcome.won,
        )
        .options(
            selectinload(Deal.request).selectinload(Request.employee),
            selectinload(Deal.chosen_supplier),
        )
        .order_by(Deal.closed_at.asc())
    )
    if batch_id is not None:
        stmt = stmt.join(Request, Request.id == Deal.request_id).where(
            Request.batch_id == batch_id
        )
    result = await session.execute(stmt)
    return list(result.scalars().all())


def _margin_total(deal: Deal) -> Decimal | None:
    if deal.final_price is None or deal.client_unit_price is None:
        return None
    qty = Decimal(deal.purchased_qty or 1)
    return (deal.client_unit_price - deal.final_price) * qty


async def build_purchase_report_telegram(
    session: AsyncSession,
    *,
    period: str,
    batch_id: int | None = None,
    on_day: date | None = None,
) -> str:
    settings = get_settings()
    now = datetime.now(UTC)
    if period == "week":
        end = now
        start = end - timedelta(days=7)
    elif period.startswith("batch"):
        start = datetime(1970, 1, 1, tzinfo=UTC)
        end = now + timedelta(days=1)
    else:
        day = on_day or now.astimezone(ZoneInfo(settings.tz)).date()
        start, end = _day_bounds(day, settings.tz)

    deals = await _load_deals_in_range(
        session, start=start, end=end, batch_id=batch_id
    )
    by_supplier: dict[str, list[Deal]] = {}
    for deal in deals:
        label = deal.chosen_supplier.name if deal.chosen_supplier else "—"
        by_supplier.setdefault(label, []).append(deal)

    blocks: list[str] = []
    title_day = on_day or now.astimezone(ZoneInfo(settings.tz)).date()
    blocks.append(render_template("purchase_report_header", day=title_day.strftime("%d.%m.%Y")))
    grand = Decimal("0")
    for supplier_name, supplier_deals in sorted(by_supplier.items()):
        subtotal = Decimal("0")
        lines: list[str] = []
        for deal in supplier_deals:
            req = deal.request
            product = req.source_text if req else "—"
            qty = deal.purchased_qty or 1
            price = deal.final_price or Decimal("0")
            subtotal += price * Decimal(qty)
            lines.append(
                render_template(
                    "purchase_report_line",
                    product=product,
                    qty=qty,
                    price=price,
                )
            )
        grand += subtotal
        blocks.append(
            render_template(
                "purchase_report_supplier_block",
                supplier_name=supplier_name,
                lines_block="\n".join(lines),
                subtotal=subtotal,
            )
        )
    blocks.append(render_template("purchase_report_grand_total", total=grand))
    return "\n\n".join(blocks)


async def build_purchase_report_csv(
    session: AsyncSession,
    *,
    start: datetime,
    end: datetime,
    batch_id: int | None = None,
) -> str:
    deals = await _load_deals_in_range(
        session, start=start, end=end, batch_id=batch_id
    )
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "closed_at",
            "batch_id",
            "request_id",
            "canonical_sku",
            "product",
            "qty",
            "supplier",
            "purchase_unit",
            "purchase_total",
            "client_unit",
            "client_total",
            "margin_total",
            "employee",
            "source_quote_id",
            "override_reason",
        ]
    )
    for deal in deals:
        req = deal.request
        qty = deal.purchased_qty or 1
        purchase_total = (deal.final_price or Decimal("0")) * Decimal(qty)
        client_total = (
            (deal.client_unit_price or Decimal("0")) * Decimal(qty)
            if deal.client_unit_price is not None
            else ""
        )
        margin = _margin_total(deal)
        employee_name = req.employee.name if req and req.employee else ""
        writer.writerow(
            [
                _csv_cell(deal.closed_at.isoformat() if deal.closed_at else ""),
                req.batch_id if req else "",
                deal.request_id,
                _csv_cell(req.canonical_sku_key or "" if req else ""),
                _csv_cell(req.source_text if req else ""),
                qty,
                _csv_cell(
                    deal.chosen_supplier.name if deal.chosen_supplier else ""
                ),
                deal.final_price,
                purchase_total,
                deal.client_unit_price or "",
                client_total,
                margin if margin is not None else "",
                _csv_cell(employee_name),
                deal.source_quote_id or "",
                _csv_cell(deal.override_reason or ""),
            ]
        )
    return buffer.getvalue()
