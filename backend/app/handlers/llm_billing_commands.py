"""Owner-only commands for managing LLM billing reminder text."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.services.app_settings_service import (
    LLM_TOPUP_PRICE_TEXT_KEY,
    get_setting,
    set_setting,
)
from app.telegram.client import TelegramClientProtocol, get_telegram_client
from app.templates.messages_ru import render_template
from app.utils.telegram import extract_message_text
from app.utils.whitelist import get_owner_by_telegram_id

MAX_PRICE_TEXT_LEN = 500


async def handle_set_llm_price(
    session: AsyncSession,
    message: dict[str, Any],
    *,
    telegram: TelegramClientProtocol | None = None,
) -> str:
    """Update the runtime LLM top-up price text.

    Does NOT require ``owner.dm_ok``; the owner is the one initiating the command.
    """
    telegram = telegram or get_telegram_client()
    from_user = message.get("from") or {}
    owner_id = int(from_user.get("id", 0))
    chat_id = int((message.get("chat") or {}).get("id", 0))

    owner = await get_owner_by_telegram_id(session, owner_id)
    if owner is None:
        return "ignored"

    text = extract_message_text(message) or ""
    # Strip "/set_llm_price" and any leading whitespace.
    price_text = text.split(" ", 1)[1].strip() if " " in text else ""

    if not price_text:
        current = await get_setting(session, LLM_TOPUP_PRICE_TEXT_KEY)
        if current is None:
            current = get_settings().llm_topup_price_text or "не задана"
        await telegram.send_message(
            chat_id,
            render_template("llm_price_usage", price_text=current),
        )
        return "ok"

    if len(price_text) > MAX_PRICE_TEXT_LEN:
        await telegram.send_message(
            chat_id,
            render_template("admin_error", detail="Слишком длинное сообщение"),
        )
        return "ok"

    await set_setting(
        session,
        LLM_TOPUP_PRICE_TEXT_KEY,
        price_text,
        updated_by=owner.id,
    )
    await session.commit()

    await telegram.send_message(
        chat_id,
        render_template("llm_price_updated", price_text=price_text),
    )
    return "ok"
