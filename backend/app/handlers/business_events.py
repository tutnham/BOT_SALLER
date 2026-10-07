"""Telegram Business connection and inbound business-message handlers."""

from __future__ import annotations

from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BusinessConnection, SupplierChat, SupplierChatType
from app.handlers.supplier_messages import handle_reply
from app.services.app_settings_service import (
    ROUTING_BUSINESS_DM_AUTOBIND_DEFAULT_KEY,
    get_setting,
)
from app.telegram.client import TelegramClientProtocol
from app.utils.whitelist import get_supplier_by_telegram_id


def _as_bool(value: Any) -> bool:
    return value is True


def _extract_rights(payload: dict[str, Any]) -> tuple[bool, bool, dict[str, Any] | None]:
    rights = payload.get("rights")
    rights_obj = rights if isinstance(rights, dict) else {}
    can_reply = rights_obj.get("can_reply")
    if can_reply is None:
        can_reply = payload.get("can_reply")
    can_read = rights_obj.get("can_read_messages")
    if can_read is None:
        can_read = payload.get("can_read_messages")
    raw_rights = rights_obj if rights_obj else None
    return _as_bool(can_reply), _as_bool(can_read), raw_rights


async def _deactivate_business_dm_routes(
    session: AsyncSession,
    business_connection_id: str,
) -> int:
    result = await session.execute(
        select(SupplierChat).where(
            SupplierChat.chat_type == SupplierChatType.business_dm,
            SupplierChat.business_connection_id == business_connection_id,
        )
    )
    deactivated = 0
    for chat in result.scalars().all():
        if chat.active or chat.is_default:
            chat.active = False
            chat.is_default = False
            deactivated += 1
    return deactivated


async def handle_business_connection(
    session: AsyncSession,
    payload: dict[str, Any],
) -> str:
    """Upsert ``business_connections`` and deactivate stale business_dm routes."""
    user = payload.get("user") or {}
    tg_user_id = user.get("id")
    connection_id = payload.get("id")
    if tg_user_id is None or not connection_id:
        logger.info("business_connection ignored: missing user.id or id")
        return "ignored"

    can_reply, can_read, raw_rights = _extract_rights(payload)
    is_enabled = payload.get("is_enabled")
    if is_enabled is None:
        is_enabled = True
    is_enabled = _as_bool(is_enabled)

    result = await session.execute(
        select(BusinessConnection).where(BusinessConnection.tg_user_id == int(tg_user_id))
    )
    row = result.scalar_one_or_none()
    old_connection_id = row.business_connection_id if row is not None else None

    if row is None:
        row = BusinessConnection(
            tg_user_id=int(tg_user_id),
            business_connection_id=str(connection_id),
            can_reply=can_reply,
            can_read_messages=can_read,
            is_enabled=is_enabled,
            raw_rights=raw_rights,
        )
        session.add(row)
    else:
        if old_connection_id is not None and old_connection_id != str(connection_id):
            await _deactivate_business_dm_routes(session, old_connection_id)
        row.business_connection_id = str(connection_id)
        row.can_reply = can_reply
        row.can_read_messages = can_read
        row.is_enabled = is_enabled
        row.raw_rights = raw_rights

    if not is_enabled or not can_reply:
        await _deactivate_business_dm_routes(session, str(connection_id))

    await session.flush()
    logger.info(
        "business_connection upsert tg_user_id={} connection_id={} enabled={} can_reply={}",
        tg_user_id,
        connection_id,
        is_enabled,
        can_reply,
    )
    return "ok"


async def handle_edited_business_message(
    session: AsyncSession,
    payload: dict[str, Any],
    *,
    telegram: TelegramClientProtocol,
) -> str:
    from sqlalchemy import select

    from app.db.models import MessageIn, Quote, Request
    from app.parsers.price_parse import parse_price_from_text
    from app.services.alert_service import notify_operators
    from app.services.supplier_review_service import ensure_review_item_for_message
    from app.services.supplier_correction_service import record_quote_correction
    from app.services.quote_service import display_price_for_group
    from app.templates.messages_ru import render_template

    connection_id = payload.get("business_connection_id")
    chat = payload.get("chat") or {}
    chat_id = chat.get("id")
    message_id = payload.get("message_id")
    new_text = (payload.get("text") or payload.get("caption") or "").strip()
    if connection_id is None or chat_id is None or message_id is None:
        return "ignored"

    message_in = await session.scalar(
        select(MessageIn).where(
            MessageIn.business_connection_id == str(connection_id),
            MessageIn.chat_id == int(chat_id),
            MessageIn.tg_message_id == int(message_id),
        )
    )
    if message_in is None:
        return "ignored"

    previous = message_in.raw_text
    message_in.raw_text_previous = previous
    message_in.raw_text = new_text[:4000]
    await session.flush()

    if message_in.bind_status != "bound" or message_in.request_id is None:
        await ensure_review_item_for_message(
            session,
            message_in=message_in,
            conflict_reason="edited_message",
        )
        return "ok"

    price_result = parse_price_from_text(new_text)
    quote = await session.scalar(
        select(Quote).where(
            Quote.request_id == message_in.request_id,
            Quote.supplier_id == message_in.supplier_id,
        )
    )
    if quote is None or price_result.price is None:
        await ensure_review_item_for_message(
            session,
            message_in=message_in,
            conflict_reason="edited_bound_no_price",
        )
        return "ok"

    if quote.price_initial == price_result.price:
        return "ok"

    old_price = quote.price_initial
    old_final = display_price_for_group(quote)
    request = await session.get(Request, message_in.request_id)
    if request is None:
        return "ignored"

    await ensure_review_item_for_message(
        session,
        message_in=message_in,
        conflict_reason="edited_price_change",
    )
    await record_quote_correction(
        session,
        quote_id=quote.id,
        old_price=old_price,
        new_price=price_result.price,
        message_in_id=message_in.id,
        reason="edit",
    )
    await notify_operators(
        session,
        render_template(
            "supplier_price_corrected_employee",
            request_id=request.id,
            old_price=old_price,
            new_price=price_result.price,
            client_old=old_final,
            client_new=old_final,
        ),
        telegram=telegram,
    )
    return "ok"


async def handle_deleted_business_messages(
    session: AsyncSession,
    payload: dict[str, Any],
    *,
    telegram: TelegramClientProtocol,
) -> str:
    from datetime import datetime, timezone

    from sqlalchemy import select

    from app.db.models import Deal, MessageIn, Quote
    from app.services.alert_service import notify_operators
    from app.templates.messages_ru import render_template

    chat = payload.get("chat") or {}
    chat_id = chat.get("id")
    message_ids = payload.get("message_ids") or []
    connection_id = payload.get("business_connection_id")
    if chat_id is None:
        return "ignored"

    for message_id in message_ids:
        stmt = select(MessageIn).where(
            MessageIn.chat_id == int(chat_id),
            MessageIn.tg_message_id == int(message_id),
        )
        if connection_id is not None:
            stmt = stmt.where(MessageIn.business_connection_id == str(connection_id))
        message_in = await session.scalar(stmt)
        if message_in is None:
            continue
        message_in.deleted_at = datetime.now(timezone.utc)
        if message_in.request_id is None:
            continue
        in_deal = await session.scalar(
            select(Deal.id).where(Deal.request_id == message_in.request_id)
        )
        quote = await session.scalar(
            select(Quote).where(
                Quote.request_id == message_in.request_id,
                Quote.supplier_id == message_in.supplier_id,
            )
        )
        if in_deal is not None:
            await notify_operators(
                session,
                render_template(
                    "alert_unbound_supplier_message",
                    supplier_label=str(message_in.supplier_id),
                    supplier_id=message_in.supplier_id,
                    raw_text=f"Удалено business-сообщение по заявке #{message_in.request_id}",
                    candidates_block="—",
                ),
                telegram=telegram,
            )
        elif quote is not None:
            await notify_operators(
                session,
                render_template(
                    "alert_unbound_supplier_message",
                    supplier_label=str(message_in.supplier_id),
                    supplier_id=message_in.supplier_id,
                    raw_text=f"Поставщик удалил сообщение по заявке #{message_in.request_id}",
                    candidates_block="—",
                ),
                telegram=telegram,
            )
    await session.flush()
    return "ok"


async def _autobind_business_dm(
    session: AsyncSession,
    *,
    supplier_id: int,
    chat_id: int,
    business_connection_id: str,
) -> None:
    result = await session.execute(
        select(SupplierChat).where(
            SupplierChat.supplier_id == supplier_id,
            SupplierChat.chat_type == SupplierChatType.business_dm,
            SupplierChat.chat_id == chat_id,
        )
    )
    chat = result.scalar_one_or_none()
    if chat is not None:
        chat.active = True
        chat.business_connection_id = business_connection_id
        await session.flush()
        return

    defaults = await session.execute(
        select(SupplierChat.id).where(
            SupplierChat.supplier_id == supplier_id,
            SupplierChat.is_default.is_(True),
            SupplierChat.active.is_(True),
        )
    )
    has_default = defaults.scalar_one_or_none() is not None
    autobind_raw = await get_setting(session, ROUTING_BUSINESS_DM_AUTOBIND_DEFAULT_KEY)
    make_default = (autobind_raw or "").lower() == "true" and not has_default
    session.add(
        SupplierChat(
            supplier_id=supplier_id,
            chat_id=chat_id,
            chat_type=SupplierChatType.business_dm,
            business_connection_id=business_connection_id,
            is_default=make_default,
            active=True,
        )
    )
    await session.flush()


async def handle_business_message(
    session: AsyncSession,
    message: dict[str, Any],
    *,
    telegram: TelegramClientProtocol,
) -> str:
    """Identify supplier, autobind business_dm, then reuse the reply pipeline."""
    connection_id = message.get("business_connection_id")
    if not connection_id:
        logger.info("business_message ignored: missing business_connection_id")
        return "ignored"

    result = await session.execute(
        select(BusinessConnection).where(
            BusinessConnection.business_connection_id == str(connection_id)
        )
    )
    connection = result.scalar_one_or_none()
    if connection is None or not connection.is_enabled:
        logger.info(
            "business_message ignored: connection missing or disabled id={}",
            connection_id,
        )
        return "ignored"

    if message.get("sender_business_bot"):
        logger.info("business_message ignored: sender_business_bot connection_id={}", connection_id)
        return "ignored"

    from_user = message.get("from") or {}
    from_id = from_user.get("id")
    if from_id is None:
        return "ignored"
    if int(from_id) == int(connection.tg_user_id):
        logger.info("business_message ignored: client own message tg_user_id={}", from_id)
        return "ignored"

    supplier = await get_supplier_by_telegram_id(session, int(from_id))
    if supplier is None:
        logger.info("business_message ignored: unknown supplier telegram_id={}", from_id)
        return "ignored"

    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    if chat_id is None:
        return "ignored"

    await _autobind_business_dm(
        session,
        supplier_id=supplier.id,
        chat_id=int(chat_id),
        business_connection_id=str(connection_id),
    )
    return await handle_reply(
        session,
        message,
        telegram=telegram,
        business_connection_id=str(connection_id),
    )
