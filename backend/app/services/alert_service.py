"""Admin alert helper for critical backend failures."""

from __future__ import annotations

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import Owner
from app.telegram.client import TelegramClientProtocol, TelegramSendError


async def send_admin_alert(
    text: str,
    *,
    telegram: TelegramClientProtocol,
) -> None:
    """Send alert to ADMIN_ALERT_CHAT_ID; no-op when unset or send fails."""
    settings = get_settings()
    chat_id = settings.admin_alert_chat_id
    if chat_id is None:
        logger.warning("ADMIN_ALERT_CHAT_ID unset; dropping admin alert")
        return
    try:
        await telegram.send_message(int(chat_id), text)
    except TelegramSendError as exc:
        logger.warning("Failed to send admin alert to chat_id={}: {}", chat_id, exc)
    except Exception as exc:
        logger.warning(
            "Unexpected error sending admin alert to chat_id={}: {}",
            chat_id,
            exc,
        )


async def notify_operators(
    session: AsyncSession,
    text: str,
    *,
    telegram: TelegramClientProtocol,
    reply_markup: dict | None = None,
) -> None:
    """Notify admin alert chat and owners with dm_ok (deduped)."""
    from app.services.price_service import resolve_price_draft_destinations

    destinations = await resolve_price_draft_destinations(session)
    settings = get_settings()
    if settings.admin_alert_chat_id is not None:
        admin_id = int(settings.admin_alert_chat_id)
        if admin_id not in destinations:
            destinations = [admin_id, *destinations]

    if not destinations:
        logger.warning("No operator destinations; dropping notify")
        return

    for chat_id in destinations:
        try:
            await telegram.send_message(int(chat_id), text, reply_markup=reply_markup)
        except TelegramSendError as exc:
            logger.warning("Failed operator notify chat_id={}: {}", chat_id, exc)


async def list_operator_destinations(session: AsyncSession) -> list[int]:
    result = await session.execute(select(Owner.telegram_id).where(Owner.dm_ok.is_(True)))
    ids = [int(row) for row in result.scalars().all()]
    settings = get_settings()
    if settings.admin_alert_chat_id is not None:
        ids.insert(0, int(settings.admin_alert_chat_id))
    seen: set[int] = set()
    unique: list[int] = []
    for chat_id in ids:
        if chat_id not in seen:
            seen.add(chat_id)
            unique.append(chat_id)
    return unique
