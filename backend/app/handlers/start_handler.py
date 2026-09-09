"""Private /start onboarding — set dm_ok for owners and suppliers (TECH DOC §5)."""

from __future__ import annotations

import re

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Supplier, SupplierBindToken
from app.services import admin_service
from app.telegram.client import TelegramClientProtocol
from app.templates.messages_ru import render_template
from app.utils.telegram import extract_message_text
from app.utils.whitelist import (
    get_employee_by_telegram_id,
    get_owner_by_telegram_id,
    get_supplier_by_telegram_id,
)

_START_RE = re.compile(
    r"^/start(?:@\w+)?(?:\s+)?(?:\s*(\S+))?\s*$",
    re.IGNORECASE,
)


def is_start_command(message: dict) -> bool:
    """True when message text is /start (with optional @bot suffix and payload)."""
    text = extract_message_text(message)
    return bool(_START_RE.match(text))


def _extract_start_payload(message: dict) -> str | None:
    text = extract_message_text(message)
    match = _START_RE.match(text)
    if match is None:
        return None
    return match.group(1)


async def handle_start(
    session: AsyncSession,
    message: dict,
    *,
    telegram: TelegramClientProtocol,
) -> str:
    """
    Handle private /start: flip dm_ok for whitelisted owner/supplier.

    Unknown users are silently ignored (no DB writes) unless a valid bind
    deep-link token is supplied.
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

    payload = _extract_start_payload(message)
    if not payload:
        return "ignored"

    result, bind_token = await admin_service.consume_supplier_bind_token(
        session, token=payload, telegram_id=tid
    )

    if result == admin_service.BindTokenResult.ok:
        assert bind_token is not None
        await session.flush()
        await telegram.send_message(cid, render_template("supplier_start_ok"))
        await _notify_owner_bind_result(
            session,
            telegram,
            bind_token=bind_token,
            telegram_id=tid,
            template="admin_supplier_bound",
        )
        return "ok"

    if result == admin_service.BindTokenResult.conflict:
        await telegram.send_message(cid, render_template("supplier_bind_conflict"))
        if bind_token is not None:
            await _notify_owner_bind_result(
                session,
                telegram,
                bind_token=bind_token,
                telegram_id=tid,
                template="admin_bind_conflict_notice",
            )
        return "ok"

    await telegram.send_message(
        cid, render_template("supplier_bind_token_invalid")
    )
    return "ok"


async def _notify_owner_bind_result(
    session: AsyncSession,
    telegram: TelegramClientProtocol,
    *,
    bind_token: SupplierBindToken,
    telegram_id: int,
    template: str,
) -> None:
    supplier = await session.get(Supplier, bind_token.supplier_id)
    supplier_name = supplier.name if supplier is not None else str(bind_token.supplier_id)
    await telegram.send_message(
        bind_token.created_by_owner_id,
        render_template(
            template,
            supplier_id=bind_token.supplier_id,
            supplier_name=supplier_name,
            telegram_id=telegram_id,
        ),
    )
