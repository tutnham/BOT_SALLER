"""Owner command handlers for analytics reports (TECH DOC §9.6, §11)."""

from __future__ import annotations

import re

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.report_service import build_report
from app.telegram.client import TelegramClientProtocol
from app.utils.telegram import extract_message_text
from app.utils.whitelist import get_owner_by_telegram_id

_REPORT_RE = re.compile(r"^/report(?:@\w+)?(?:\s+(.+))?\s*$", re.IGNORECASE)
_STATS_RE = re.compile(r"^/stats(?:@\w+)?(?:\s+(.+))?\s*$", re.IGNORECASE)


def _parse_report_args(text: str) -> tuple[str, int | None] | None:
    report_match = _REPORT_RE.match(text)
    if report_match:
        arg = (report_match.group(1) or "day").strip().lower()
        if arg in {"day", "week"}:
            return arg, None
        if arg.isdigit():
            return "day", int(arg)
        return None

    stats_match = _STATS_RE.match(text)
    if stats_match:
        arg = (stats_match.group(1) or "week").strip().lower()
        if arg == "week":
            return "week", None
        return None

    return None


async def handle_owner_message(
    session: AsyncSession,
    message: dict,
    *,
    telegram: TelegramClientProtocol,
) -> str:
    from_user = message.get("from") or {}
    telegram_id = from_user.get("id")
    if telegram_id is None:
        return "ignored"

    owner = await get_owner_by_telegram_id(session, int(telegram_id))
    if owner is None:
        return "ignored"
    if not owner.dm_ok:
        return "ignored"

    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    text = extract_message_text(message)
    if chat_id is None or not text:
        return "ignored"

    parsed = _parse_report_args(text)
    if parsed is None:
        await telegram.send_message(
            int(chat_id),
            "Неверный формат команды. Используйте /report day, /report week, /report {id} или /stats week.",
        )
        return "ok"

    period, request_id = parsed
    report_text = await build_report(session, period=period, request_id=request_id)
    await telegram.send_message(int(chat_id), report_text)
    return "ok"
