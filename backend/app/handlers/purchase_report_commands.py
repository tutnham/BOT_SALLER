"""Owner/employee procurement report commands."""

from __future__ import annotations

import re
from datetime import date, datetime
from html import escape
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.services.purchase_report_service import (
    _resolve_range,
    build_purchase_report_csv,
    build_purchase_report_telegram,
)
from app.telegram.client import TelegramClientProtocol
from app.telegram.keyboards import inline_keyboard, menu_button
from app.utils.html import TELEGRAM_HTML_PARSE_MODE
from app.utils.whitelist import get_employee_by_telegram_id, get_owner_by_telegram_id

_PURCHASE_REPORT_RE = re.compile(
    r"^/purchase_report(?:@\w+)?(?:\s+(.+))?\s*$",
    re.IGNORECASE,
)


def purchase_report_period_keyboard() -> dict[str, object]:
    return inline_keyboard(
        [
            [
                menu_button("Сегодня", "report_purchases"),
                menu_button("Вчера", "report_purchases_yest"),
                menu_button("7 дней", "report_purchases_week"),
            ],
        ]
    )


async def send_purchase_report(
    session: AsyncSession,
    *,
    chat_id: int,
    telegram: TelegramClientProtocol,
    period: str = "day",
    on_day: date | None = None,
    batch_id: int | None = None,
    attach_csv: bool = False,
    message_id: int | None = None,
) -> None:
    """Render and send the HTML purchase report with period switcher buttons."""
    report = await build_purchase_report_telegram(
        session,
        period=period,
        batch_id=batch_id,
        on_day=on_day,
    )
    markup = purchase_report_period_keyboard()
    if message_id is not None:
        try:
            await telegram.edit_message_text(
                chat_id,
                message_id,
                report,
                parse_mode=TELEGRAM_HTML_PARSE_MODE,
                reply_markup=markup,
            )
        except Exception:
            await telegram.send_message(
                chat_id,
                report,
                parse_mode=TELEGRAM_HTML_PARSE_MODE,
                reply_markup=markup,
            )
    else:
        await telegram.send_message(
            chat_id,
            report,
            parse_mode=TELEGRAM_HTML_PARSE_MODE,
            reply_markup=markup,
        )

    if not attach_csv:
        return

    settings = get_settings()
    start, end, _, _ = _resolve_range(
        period=period,
        on_day=on_day,
        tz_name=settings.tz,
    )
    csv_body = await build_purchase_report_csv(
        session, start=start, end=end, batch_id=batch_id
    )
    if csv_body.count("\n") <= 1:
        return
    if len(csv_body) >= 3500:
        return
    safe_body = escape(csv_body[:3000])
    await telegram.send_message(
        chat_id,
        f"<pre>{safe_body}</pre>",
        parse_mode=TELEGRAM_HTML_PARSE_MODE,
    )


async def handle_purchase_report_command(
    session: AsyncSession,
    *,
    text: str,
    chat_id: int,
    telegram_id: int,
    telegram: TelegramClientProtocol,
    attach_csv: bool = False,
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

    await send_purchase_report(
        session,
        chat_id=chat_id,
        telegram=telegram,
        period=period,
        batch_id=batch_id,
        on_day=on_day,
        attach_csv=attach_csv and owner is not None,
    )
    return True
