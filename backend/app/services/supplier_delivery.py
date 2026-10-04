"""Per-supplier Telegram send with messages_out audit (sent or failed)."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import MessageKind, MessageOut, MessageSendStatus
from app.services.outbox_service import enqueue_telegram_message
from app.telegram.client import TelegramClientProtocol, TelegramSendError


async def deliver(
    session: AsyncSession,
    telegram: TelegramClientProtocol,
    *,
    supplier_id: int,
    chat_id: int,
    text: str,
    kind: MessageKind,
    request_id: int | None,
    business_connection_id: str | None = None,
) -> MessageOut:
    """Send one template message and persist ``messages_out``.

    A Bot API failure is stored as ``send_status='failed'`` and does not raise.
    When ``OUTBOX_DELIVERY_ENABLED``, enqueue for the worker instead of Bot API.
    """
    if get_settings().outbox_delivery_enabled:
        dedupe_key = f"{kind.value}:{request_id}:{supplier_id}:{hash(text)}"
        outbound, _is_new = await enqueue_telegram_message(
            session,
            dedupe_key=dedupe_key,
            chat_id=chat_id,
            text=text,
            kind=kind,
            request_id=request_id,
            supplier_id=supplier_id,
            business_connection_id=business_connection_id,
        )
        return outbound

    try:
        message_id = await telegram.send_message(
            chat_id,
            text,
            business_connection_id=business_connection_id,
        )
        outbound = MessageOut(
            request_id=request_id,
            supplier_id=supplier_id,
            tg_message_id=message_id,
            chat_id=chat_id,
            text=text,
            kind=kind,
            business_connection_id=business_connection_id,
            send_status=MessageSendStatus.sent.value,
            error_text=None,
        )
    except TelegramSendError as exc:
        outbound = MessageOut(
            request_id=request_id,
            supplier_id=supplier_id,
            tg_message_id=None,
            chat_id=chat_id,
            text=text,
            kind=kind,
            business_connection_id=business_connection_id,
            send_status=MessageSendStatus.failed.value,
            error_text=exc.description,
        )
    session.add(outbound)
    await session.flush()
    return outbound


def is_sent(outbound: MessageOut) -> bool:
    return outbound.send_status == MessageSendStatus.sent.value


def is_failed_business_peer(outbound: MessageOut) -> bool:
    """True when stored error looks like BUSINESS_PEER_USAGE_MISSING / 24h window."""
    if outbound.send_status != MessageSendStatus.failed.value:
        return False
    description = (outbound.error_text or "").lower()
    return "recently" in description or "business_peer_usage_missing" in description
