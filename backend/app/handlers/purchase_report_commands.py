"""Owner/employee procurement report commands."""

from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.services.purchase_report_service import (
    _day_bounds,
    build_purchase_report_csv,
    build_purchase_report_telegram,
)
from app.telegram.client import TelegramClientProtocol
from app.utils.whitelist import get_employee_by_telegram_id, get_owner_by_telegram_id

_PURCHASE_REPORT_RE = re.compile(
    r"^/purchase_report(?:@\w+)?(?:\s+(.+))?\s*$",
    re.IGNORECASE,
)


async def handle_purchase_report_command(
    session: AsyncSession,
    *,
    text: str,
    chat_id: int,
    telegram_id: int,
    telegram: TelegramClientProtocol,
) -> bool:
    match = _PURCHASE_REPORT_RE.match(text.strip())
    if not match:
        return False

    owner = await get_owner_by_telegram_id(session, telegram_id)
    employee = await get_employee_by_telegram_id(session, telegram_id, require_active=True)
    if owner is None and employee is None:
        return True

    arg = (match.group(1) or "today").strip().lower()
    batch_id: int | None = None
    period = "day"
    on_day = None
    if arg == "week":
        period = "week"
    elif arg.startswith("batch"):
        parts = arg.split()
        if len(parts) >= 2 and parts[1].isdigit():
            batch_id = int(parts[1])
            period = "batch"
    elif re.match(r"\d{4}-\d{2}-\d{2}", arg):
        on_day = datetime.strptime(arg, "%Y-%m-%d").date()
    else:
        settings = get_settings()
        on_day = datetime.now(ZoneInfo(settings.tz)).date()

    report = await build_purchase_report_telegram(
        session,
        period=period,
        batch_id=batch_id,
        on_day=on_day,
    )
    await telegram.send_message(chat_id, report)
    if owner is not None and on_day is not None:
        settings = get_settings()
        start, end = _day_bounds(on_day, settings.tz)
        csv_body = await build_purchase_report_csv(
            session, start=start, end=end, batch_id=batch_id
        )
        if len(csv_body) < 3500:
            await telegram.send_message(chat_id, f"<pre>{csv_body[:3000]}</pre>")
    return True
