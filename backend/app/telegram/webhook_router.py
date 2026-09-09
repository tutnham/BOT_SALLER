"""Telegram webhook dispatcher (TECH DOC §7.1)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from loguru import logger
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.session import get_db
from app.handlers.admin_menu import handle_admin_callback, handle_admin_message
from app.handlers.chat_events import handle_my_chat_member
from app.handlers.employee_commands import handle_employee_message
from app.handlers.llm_billing_commands import handle_set_llm_price
from app.handlers.owner_commands import handle_owner_message
from app.handlers.price_approval import handle_price_callback, handle_price_command
from app.handlers.start_handler import handle_start, is_start_command
from app.handlers.supplier_messages import handle_reply
from app.services import admin_service
from app.services.alert_service import send_admin_alert
from app.services.price_service import is_price_command_chat
from app.services.routing_service import chat_role
from app.telegram.client import get_telegram_client
from app.telegram.deps import verify_telegram_secret_token
from app.telegram.keyboards import CallbackData
from app.utils.idempotency import is_duplicate_update, mark_update_processed
from app.utils.telegram import extract_message_text
from app.utils.whitelist import (
    is_owner,
    is_supplier,
    resolve_price_approver,
)

_PRICE_COMMAND_PREFIXES = ("/approve_price", "/reject_price")

router = APIRouter(prefix="/telegram", tags=["telegram"])
_GROUP_TYPES = frozenset({"group", "supergroup"})


class TelegramWebhookUpdate(BaseModel):
    """Validated webhook envelope. Unknown fields preserved for handlers."""

    model_config = ConfigDict(extra="allow")
    update_id: int | None = None
    message: dict[str, Any] | None = None
    callback_query: dict[str, Any] | None = None
    my_chat_member: dict[str, Any] | None = None


def _get_message(update: dict[str, Any]) -> dict[str, Any] | None:
    """Return ``message`` only; ``edited_message`` is ignored in Phase 1."""
    return update.get("message")


def _message_text(message: dict[str, Any]) -> str:
    text = extract_message_text(message)
    max_len = get_settings().max_message_text_len
    if len(text) > max_len:
        return text[:max_len]
    return text


def _is_reply_to_bot(message: dict[str, Any]) -> bool:
    reply = message.get("reply_to_message") or {}
    from_user = reply.get("from") or {}
    return bool(from_user.get("is_bot"))


def _mentions_bot(message: dict[str, Any]) -> bool:
    """True when message @mentions configured bot username."""
    username = (get_settings().telegram_bot_username or "").lstrip("@").lower()
    if not username:
        return False

    text = _message_text(message)
    if f"@{username}" in text.lower():
        return True

    entities = message.get("entities") or message.get("caption_entities") or []
    for entity in entities:
        if entity.get("type") != "mention":
            continue
        offset = int(entity.get("offset", 0))
        length = int(entity.get("length", 0))
        mention = text[offset : offset + length].lstrip("@").lower()
        if mention == username:
            return True
    return False


def _is_nl_gated_group_message(message: dict[str, Any]) -> bool:
    """Privacy-Mode-safe gate for NL synonyms: reply-to-bot or @mention."""
    text = _message_text(message)
    if not text or text.startswith("/"):
        return False
    return _is_reply_to_bot(message) or _mentions_bot(message)


async def _route_message(
    session: AsyncSession,
    message: dict[str, Any],
) -> str:
    chat = message.get("chat") or {}
    chat_type = chat.get("chat_type") or chat.get("type")
    text = _message_text(message)
    from_user = message.get("from") or {}
    from_id = from_user.get("id")
    if from_id is None:
        return "ignored"

    telegram = get_telegram_client()

    if chat_type in _GROUP_TYPES:
        chat_id = chat.get("id")
        if chat_id is None:
            return "ignored"
        if text.startswith(_PRICE_COMMAND_PREFIXES):
            role = await chat_role(session, int(chat_id))
            if role == "client_group" or await is_price_command_chat(
                session, int(chat_id)
            ):
                approver = await resolve_price_approver(session, int(from_id))
                if approver is not None:
                    return await handle_price_command(
                        session,
                        message,
                        approver=approver,
                        telegram=telegram,
                    )
            return "ignored"
        role = await chat_role(session, int(chat_id))
        if role == "client_group":
            if text.startswith("/") or _is_nl_gated_group_message(message):
                return await handle_employee_message(session, message, telegram=telegram)
            return "ignored"
        if role == "supplier_chat":
            return await handle_reply(session, message, telegram=telegram)
        # Unknown group: allow owner to bind it via pending flow, otherwise ignore.
        if await is_owner(session, int(from_id)) and text.startswith("/"):
            return await handle_admin_message(session, message, telegram=telegram)
        return "ignored"

    if chat_type == "private":
        if is_start_command(message):
            return await handle_start(session, message, telegram=telegram)

        if await is_supplier(session, int(from_id)):
            return await handle_reply(session, message, telegram=telegram)

        if text.startswith(_PRICE_COMMAND_PREFIXES):
            approver = await resolve_price_approver(session, int(from_id))
            if approver is not None:
                return await handle_price_command(
                    session,
                    message,
                    approver=approver,
                    telegram=telegram,
                )

        if await is_owner(session, int(from_id)):
            if text.startswith("/menu") or text.startswith("/admin"):
                return await handle_admin_message(session, message, telegram=telegram)
            if text.startswith("/set_llm_price"):
                return await handle_set_llm_price(session, message, telegram=telegram)
            if text.startswith("/purge_request") or text.startswith("/purge_old"):
                return await handle_owner_message(session, message, telegram=telegram)
            if text.startswith("/report") or text.startswith("/stats"):
                return await handle_owner_message(session, message, telegram=telegram)
            if text.startswith("/"):
                # any unrecognized slash command from owner also falls back to admin
                return await handle_admin_message(session, message, telegram=telegram)
            if await admin_service.get_dialog(session, int(from_id)) is not None:
                return await handle_admin_message(session, message, telegram=telegram)

    return "ignored"


async def _route_callback(
    session: AsyncSession,
    callback_query: dict[str, Any],
) -> str:
    telegram = get_telegram_client()
    from_user = callback_query.get("from") or {}
    from_id = from_user.get("id")
    if from_id is None:
        return "ignored"

    cd = CallbackData.decode(callback_query.get("data"))
    if cd is None:
        return "ignored"

    callback_id = callback_query.get("id", "")

    if cd.namespace == "admin":
        if not await is_owner(session, int(from_id)):
            await telegram.answer_callback_query(callback_id, text="Нет доступа")
            return "ignored"
        return await handle_admin_callback(session, callback_query)

    if cd.namespace == "price":
        if await resolve_price_approver(session, int(from_id)) is None:
            await telegram.answer_callback_query(callback_id, text="Нет доступа")
            return "ignored"
        return await handle_price_callback(session, callback_query)

    return "ignored"


async def _route_my_chat_member(
    session: AsyncSession,
    my_chat_member: dict[str, Any],
) -> str:
    return await handle_my_chat_member(session, my_chat_member)


async def _route_update(
    session: AsyncSession,
    update: dict[str, Any],
) -> str:
    if update.get("callback_query") is not None:
        return await _route_callback(session, update["callback_query"])
    if update.get("my_chat_member") is not None:
        return await _route_my_chat_member(session, update["my_chat_member"])

    message = _get_message(update)
    if message is not None:
        return await _route_message(session, message)
    return "ignored"


@router.post("/webhook", dependencies=[Depends(verify_telegram_secret_token)])
async def telegram_webhook(
    update: TelegramWebhookUpdate,
    session: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    """
    Dispatch Telegram Update from Telegram webhook.

    Always returns HTTP 200 except when ``verify_telegram_secret_token`` rejects (401).
    """
    update_data = update.model_dump(mode="python")
    update_id = update.update_id
    if update_id is None:
        return {"status": "ignored"}

    if await is_duplicate_update(session, int(update_id)):
        return {"status": "duplicate"}

    if await mark_update_processed(session, int(update_id)):
        await session.commit()
        return {"status": "duplicate"}
    await session.commit()

    try:
        status = await _route_update(session, update_data)
        await session.commit()
        return {"status": status}
    except Exception as exc:
        await session.rollback()
        logger.exception(
            "Webhook processing failed update_id={} error_type={}",
            update_id,
            type(exc).__name__,
        )
        telegram = get_telegram_client()
        await send_admin_alert(
            f"⚠️ Backend webhook error\n"
            f"Error: {type(exc).__name__}\n"
            f"Update ID: {update_id if update_id is not None else 'n/a'}",
            telegram=telegram,
        )
        return {"status": "error"}
