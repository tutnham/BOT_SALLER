"""Admin alert helper for critical backend failures."""

from __future__ import annotations

from loguru import logger

from app.config import get_settings
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
