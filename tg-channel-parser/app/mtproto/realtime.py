"""Realtime listener for active channels (doc §6.2).

Usage:
  python -m app.mtproto.realtime
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone

from loguru import logger
from pyrogram import filters
from pyrogram.handlers import MessageHandler
from sqlalchemy import select

from app.config import get_settings
from app.db.models import ParserChannel, ParserChannelStatus
from app.db.session import dispose_engine, get_session_factory
from app.logging_setup import setup_logging
from app.mtproto.client_factory import create_client
from app.mtproto.upsert import upsert_post


async def _load_active_channel_ids() -> list[int]:
    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(
            select(ParserChannel.channel_id).where(
                ParserChannel.is_active.is_(True),
                ParserChannel.status == ParserChannelStatus.active,
                ParserChannel.channel_id.is_not(None),
            )
        )
        return list(result.scalars().all())


async def run_realtime() -> None:
    settings = get_settings()
    setup_logging(settings.log_level, settings.secret_values())
    settings.require_mtproto()

    channel_ids = await _load_active_channel_ids()
    if not channel_ids:
        logger.critical(
            "No active channels in parser_channels — register via add_channel or POST /channels"
        )
        print("WARNING: no active channels; sleeping forever", file=sys.stderr)
        while True:
            await asyncio.sleep(3600)

    logger.info("Realtime listening channels={}", channel_ids)
    client = create_client("listener", settings=settings)
    handler = MessageHandler(_on_new_post, filters.chat(channel_ids))
    client.add_handler(handler)

    try:
        await client.start()
        logger.info("tg-listener started")
        await _reload_loop(client, handler, settings)
    except Exception:
        logger.critical("Listener auth/runtime failure — stop process")
        raise
    finally:
        try:
            await client.stop()
        except Exception:
            pass
        await dispose_engine()


async def _on_new_post(_client, message) -> None:  # noqa: ANN001
    factory = get_session_factory()
    try:
        async with factory() as session:
            post = await upsert_post(session, message)
            await session.commit()
            if post is not None:
                logger.info(
                    "New post id={} channel={} message_id={}",
                    post.id,
                    post.channel_id,
                    post.message_id,
                )
    except Exception:
        logger.exception(
            "Failed to upsert message_id={} chat={}",
            getattr(message, "id", None),
            getattr(getattr(message, "chat", None), "id", None),
        )


async def _reload_loop(
    client,
    handler,
    settings,
) -> None:
    """Periodically refresh the channel filter without restarting the process."""
    interval = getattr(settings, "listener_reload_interval_seconds", 30)
    last_ids: frozenset[int] = frozenset()
    while True:
        await asyncio.sleep(interval)
        try:
            new_ids = frozenset(await _load_active_channel_ids())
            if new_ids == last_ids:
                continue
            logger.info("Reloading listener channel filter channels={}", sorted(new_ids))
            client.remove_handler(handler)
            handler = MessageHandler(_on_new_post, filters.chat(list(new_ids)))
            client.add_handler(handler)
            last_ids = new_ids
        except Exception:
            logger.exception("Failed to reload listener channel filter")


def main() -> None:
    try:
        asyncio.run(run_realtime())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
