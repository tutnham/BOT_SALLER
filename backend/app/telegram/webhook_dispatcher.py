"""Shared Telegram update routing for webhook and inbox worker."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.telegram.webhook_router import _route_update


async def process_telegram_update(
    session: AsyncSession,
    update: dict[str, Any],
) -> str:
    """Run the same dispatcher as the synchronous webhook path."""
    return await _route_update(session, update)
