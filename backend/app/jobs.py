"""Reusable job runners for cron and manual /jobs/* endpoints."""

from __future__ import annotations

from typing import Any, Literal

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import Owner
from app.services.price_service import build_morning_price, notify_morning_price_draft
from app.services.recheck_service import send_due_rechecks
from app.services.report_service import build_report
from app.telegram.client import TelegramClientProtocol, TelegramSendError


async def _resolve_report_destinations(session: AsyncSession) -> list[int]:
    result = await session.execute(
        select(Owner.telegram_id).where(Owner.dm_ok.is_(True))
    )
    owner_chat_ids = [int(chat_id) for chat_id in result.scalars().all()]
    if owner_chat_ids:
        return owner_chat_ids

    analytics_chat_id = get_settings().analytics_chat_id
    if analytics_chat_id is None:
        return []
    return [int(analytics_chat_id)]


async def run_morning_price(
    session: AsyncSession,
    telegram: TelegramClientProtocol,
) -> dict[str, Any]:
    result = await build_morning_price(session, telegram)
    items_block = result.pop("approval_items_block", None)
    draft_id = result.get("draft_id")
    reused = bool(result.get("reused"))
    # Commit draft before Telegram notify so timeouts cannot orphan draft_id.
    await session.commit()
    if draft_id is not None and not reused and items_block is not None:
        await notify_morning_price_draft(
            telegram,
            draft_id=int(draft_id),
            items_block=str(items_block),
        )
    return result


async def run_recheck_due(
    session: AsyncSession,
    telegram: TelegramClientProtocol,
) -> dict[str, Any]:
    result = await send_due_rechecks(session, telegram)
    await session.commit()
    return result


async def run_daily_report(
    session: AsyncSession,
    telegram: TelegramClientProtocol,
    *,
    period: Literal["day", "week"],
) -> dict[str, int | str]:
    report_text = await build_report(session, period=period)
    await session.commit()

    destinations = await _resolve_report_destinations(session)
    if not destinations:
        logger.warning(
            "Daily report period={} built but no destinations "
            "(owners with dm_ok or ANALYTICS_CHAT_ID)",
            period,
        )
        return {
            "status": "ok",
            "period": period,
            "sent": 0,
            "failed": 0,
        }

    sent = 0
    failed = 0
    for chat_id in destinations:
        try:
            await telegram.send_message(chat_id, report_text)
            sent += 1
        except TelegramSendError:
            failed += 1

    if failed > 0:
        logger.warning(
            "Daily report period={} partial send failure sent={} failed={}",
            period,
            sent,
            failed,
        )

    return {
        "status": "ok",
        "period": period,
        "sent": sent,
        "failed": failed,
    }
