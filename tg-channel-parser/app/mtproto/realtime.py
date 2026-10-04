"""Realtime listener for active channels (doc §6.2).

Usage:
  python -m app.mtproto.realtime
"""

from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger
from pyrogram import Client, filters
from pyrogram.handlers import MessageHandler
from sqlalchemy import select

from app.config import Settings
from app.db.models import ParserChannel, ParserChannelStatus
from app.db.session import get_session_factory
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
        return [cid for cid in result.scalars().all() if cid is not None]


def _build_handler(channel_ids: list[int]) -> MessageHandler | None:
    if not channel_ids:
        return None
    chat_filter: list[int | str] = list(channel_ids)
    return MessageHandler(_on_new_post, filters.chat(chat_filter))


async def run_realtime() -> None:
    """Deprecated: use ``app.mtproto.runtime.run_mtproto_runtime``."""
    from app.mtproto.runtime import run_mtproto_runtime

    await run_mtproto_runtime()


async def _on_new_post(_client: Client, message: Any) -> None:
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
    client: Client,
    handler: MessageHandler | None,
    settings: Settings,
) -> None:
    """Periodically refresh the channel filter without restarting the process."""
    interval = settings.listener_reload_interval_seconds
    last_ids: frozenset[int] = frozenset()
    while True:
        await asyncio.sleep(interval)
        try:
            new_ids = frozenset(await _load_active_channel_ids())
            if new_ids == last_ids:
                continue
            logger.info("Reloading listener channel filter channels={}", sorted(new_ids))
            if handler is not None:
                client.remove_handler(handler)
            handler = _build_handler(list(new_ids))
            if handler is not None:
                client.add_handler(handler)
            last_ids = new_ids
        except Exception:
            logger.exception("Failed to reload listener channel filter")


def main() -> None:
    from app.mtproto.runtime import main as runtime_main

    runtime_main()


if __name__ == "__main__":
    main()
