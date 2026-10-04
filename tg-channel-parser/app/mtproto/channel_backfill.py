"""Bounded history sync after a channel becomes active."""

from __future__ import annotations

from datetime import UTC
from typing import Any

from loguru import logger
from sqlalchemy import func, select, update

from app.config import Settings, get_settings
from app.db.models import ParserChannel, ParserPost
from app.db.session import get_session_factory
from app.mtproto.upsert import upsert_post


async def backfill_channel_on_activate(
    client: Any,
    *,
    channel: ParserChannel,
    settings: Settings | None = None,
) -> int:
    """Pull recent channel history into parser_posts (idempotent upsert)."""
    cfg = settings or get_settings()
    if channel.channel_id is None:
        return 0

    chat_id = channel.channel_id
    kwargs: dict[str, Any] = {"chat_id": chat_id}
    if channel.last_synced_message_id is not None:
        kwargs["offset_id"] = channel.last_synced_message_id

    inserted = 0
    limit = cfg.backfill_on_activate_limit
    factory = get_session_factory()
    history = client.get_chat_history(**kwargs)
    if history is None:
        logger.warning("Backfill skipped: empty history channel={}", chat_id)
        return 0

    async for message in history:
        if inserted >= limit:
            break
        msg_date = message.date
        if msg_date and msg_date.tzinfo is None:
            msg_date = msg_date.replace(tzinfo=UTC)
        async with factory() as session:
            post = await upsert_post(session, message)
            if post is not None:
                inserted += 1
            await session.commit()

    async with factory() as session:
        max_id = await session.scalar(
            select(func.max(ParserPost.message_id)).where(
                ParserPost.channel_id == chat_id
            )
        )
        if max_id is not None:
            await session.execute(
                update(ParserChannel)
                .where(ParserChannel.id == channel.id)
                .values(last_synced_message_id=max_id)
            )
            await session.commit()

    logger.info(
        "Backfill on activate channel={} inserted={} limit={}",
        chat_id,
        inserted,
        limit,
    )
    return inserted
