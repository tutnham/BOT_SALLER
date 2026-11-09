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
from app.templates.report_fallback import _format_money
from app.utils.html import sanitize_telegram_html

_PURCHASE_REPORT_MAX_LEN = 3900


def _csv_cell(value: str) -> str:
    if value and value[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def _day_bounds(day: date, tz_name: str) -> tuple[datetime, datetime]:
    tz = ZoneInfo(tz_name)
    start = datetime.combine(day, time.min, tzinfo=tz).astimezone(UTC)
    end = start + timedelta(days=1)
    return start, end


def _resolve_range(
    *,
    period: str,
    on_day: date | None,
    tz_name: str,
) -> tuple[datetime, datetime, str, str]:
    """Return UTC bounds, period title, and empty-state hint."""
    now = datetime.now(UTC)
    tz = ZoneInfo(tz_name)
    today = now.astimezone(tz).date()

    if period == "week":
        end = now
        start = end - timedelta(days=7)
        start_local = start.astimezone(tz).date()
        end_local = end.astimezone(tz).date()
        period_label = (
            f"{start_local.strftime('%d.%m')}–{end_local.strftime('%d.%m.%Y')}"
        )
        empty_hint = "За последние 7 дней закрытых закупок нет."
        return start, end, period_label, empty_hint

    if period.startswith("batch"):
        start = datetime(1970, 1, 1, tzinfo=UTC)
        end = now + timedelta(days=1)
        period_label = "пакетная заявка"
        empty_hint = "За выбранный пакет закрытых закупок нет."
        return start, end, period_label, empty_hint

    day = on_day or today
    start, end = _day_bounds(day, tz_name)
    period_label = day.strftime("%d.%m.%Y")
    if on_day is not None and on_day < today:
        empty_hint = f"За {period_label} закрытых закупок нет."
    else:
        empty_hint = "За этот день закрытых закупок нет."
    return start, end, period_label, empty_hint


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


def _format_margin(deal: Deal) -> str:
    margin = _margin_total(deal)
    if margin is None:
        return "—"
    return _format_money(margin)


def _render_purchase_report_html(
    deals: list[Deal],
    *,
    period_label: str,
    empty_hint: str,
) -> str:
    blocks: list[str] = [
        render_template("purchase_report_header", period_label=period_label),
    ]
    if not deals:
        blocks.append(render_template("purchase_report_empty", empty_hint=empty_hint))
        return "\n\n".join(blocks)

    blocks.append(
        render_template("purchase_report_positions", count=len(deals))
    )

    by_supplier: dict[str, list[Deal]] = {}
    for deal in deals:
        raw_name = deal.chosen_supplier.name if deal.chosen_supplier else "—"
        by_supplier.setdefault(raw_name, []).append(deal)

    grand = Decimal("0")
    margin_grand = Decimal("0")
    has_margin = False
    hidden_positions = 0

    for supplier_name, supplier_deals in sorted(by_supplier.items()):
        subtotal = Decimal("0")
        lines: list[str] = []
        for deal in supplier_deals:
            req = deal.request
            product_raw = (req.source_text if req else "") or "—"
            product = sanitize_telegram_html(product_raw)
            qty = deal.purchased_qty or 1
            unit = deal.final_price or Decimal("0")
            client_unit = deal.client_unit_price
            client_price = (
                _format_money(client_unit) if client_unit is not None else "—"
            )
            margin_line = _format_margin(deal)
            deal_margin = _margin_total(deal)
            if deal_margin is not None:
                margin_grand += deal_margin
                has_margin = True

            line = render_template(
                "purchase_report_line",
                product=product,
                qty=qty,
                price=_format_money(unit),
                client_price=client_price,
                margin=margin_line,
            )
            trial_subtotal = subtotal + unit * Decimal(qty)
            supplier_block = render_template(
                "purchase_report_supplier_block",
                supplier_name=sanitize_telegram_html(supplier_name),
                lines_block="\n".join(lines + [line]),
                subtotal=_format_money(trial_subtotal),
            )
            candidate = "\n\n".join(blocks + [supplier_block])
            if len(candidate) > _PURCHASE_REPORT_MAX_LEN:
                hidden_positions += 1
                continue
            lines.append(line)
            subtotal = trial_subtotal

        if not lines:
            continue

        grand += subtotal
        blocks.append(
            render_template(
                "purchase_report_supplier_block",
                supplier_name=sanitize_telegram_html(supplier_name),
                lines_block="\n".join(lines),
                subtotal=_format_money(subtotal),
            )
        )

    if hidden_positions:
        blocks.append(
            render_template(
                "purchase_report_truncated",
                count=hidden_positions,
            )
        )

    blocks.append(
        render_template("purchase_report_grand_total", total=_format_money(grand))
    )
    if has_margin:
        blocks.append(
            render_template(
                "purchase_report_margin_total",
                margin=_format_money(margin_grand),
            )
        )
    return "\n\n".join(blocks)


async def build_purchase_report_telegram(
    session: AsyncSession,
    *,
    period: str,
    batch_id: int | None = None,
    on_day: date | None = None,
) -> str:
    settings = get_settings()
    start, end, period_label, empty_hint = _resolve_range(
        period=period,
        on_day=on_day,
        tz_name=settings.tz,
    )
    deals = await _load_deals_in_range(
        session, start=start, end=end, batch_id=batch_id
    )
    return _render_purchase_report_html(
        deals,
        period_label=period_label,
        empty_hint=empty_hint,
    )


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
                _csv_cell(employee_name or ""),
                deal.source_quote_id or "",
                _csv_cell(deal.override_reason or ""),
            ]
        )
    return buffer.getvalue()
