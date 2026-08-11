"""Private /start onboarding — set dm_ok for owners and suppliers (TECH DOC §5)."""

from __future__ import annotations

import re

from sqlalchemy.ext.asyncio import AsyncSession

from app.telegram.client import TelegramClientProtocol
from app.templates.messages_ru import render_template
from app.utils.telegram import extract_message_text
from app.utils.whitelist import (
    get_employee_by_telegram_id,
    get_owner_by_telegram_id,
    get_supplier_by_telegram_id,
)

_START_RE = re.compile(r"^/start(?:@\w+)?(?:\s|$)", re.IGNORECASE)


def is_start_command(message: dict) -> bool:
    """True when message text is /start (with optional @bot suffix)."""
    text = extract_message_text(message)
    return bool(_START_RE.match(text))


async def handle_start(
    session: AsyncSession,
    message: dict,
    *,
    telegram: TelegramClientProtocol,
) -> str:
    """
    Handle private /start: flip dm_ok for whitelisted owner/supplier.

    Unknown users are silently ignored (no DB writes).
    """
    from_user = message.get("from") or {}
    telegram_id = from_user.get("id")
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    if telegram_id is None or chat_id is None:
        return "ignored"

    tid = int(telegram_id)
    cid = int(chat_id)

    owner = await get_owner_by_telegram_id(session, tid)
    if owner is not None:
        owner.dm_ok = True
        await session.flush()
        await telegram.send_message(cid, render_template("owner_start_ok"))
        return "ok"

    supplier = await get_supplier_by_telegram_id(session, tid)
    if supplier is not None:
        supplier.dm_ok = True
        await session.flush()
        await telegram.send_message(cid, render_template("supplier_start_ok"))
        return "ok"

    employee = await get_employee_by_telegram_id(session, tid, require_active=True)
    if employee is not None:
        await telegram.send_message(cid, render_template("help"))
        return "ok"

    return "ignored"
