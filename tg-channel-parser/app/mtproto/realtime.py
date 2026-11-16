"""Realtime listener for active channels (doc §6.2).

Usage:
  python -m app.mtproto.realtime
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from typing import Any

from loguru import logger
from pyrogram import Client, filters
from pyrogram.handlers import (
    DeletedMessagesHandler,
    EditedMessageHandler,
    MessageHandler,
)
from sqlalchemy import select

from app.config import Settings
from app.db.models import ParserChannel, ParserChannelStatus
from app.db.session import get_session_factory
from app.mtproto.upsert import apply_post_edit, mark_posts_deleted, upsert_post


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


def _build_handlers(channel_ids: list[int]) -> list[Any]:
    """Новые посты + редактирования + удаления (B1)."""
    if not channel_ids:
        return []
    chat_filter: list[int | str] = list(channel_ids)
    return [
        MessageHandler(_on_new_post, filters.chat(chat_filter)),
        EditedMessageHandler(_on_edited_post, filters.chat(chat_filter)),
        DeletedMessagesHandler(_on_deleted_messages, filters.chat(chat_filter)),
    ]


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


async def _on_edited_post(_client: Client, message: Any) -> None:
    factory = get_session_factory()
    try:
        async with factory() as session:
            post = await apply_post_edit(session, message)
            await session.commit()
            if post is not None:
                logger.info(
                    "Post edited id={} channel={} message_id={}",
                    post.id,
                    post.channel_id,
                    post.message_id,
                )
    except Exception:
        logger.exception(
            "Failed to apply edit message_id={} chat={}",
            getattr(message, "id", None),
            getattr(getattr(message, "chat", None), "id", None),
        )


async def _on_deleted_messages(_client: Client, messages: Any) -> None:
    # Pyrogram передаёт список Message только с id/chat — группируем по каналу
    by_chat: dict[int, list[int]] = {}
    for message in messages or []:
        chat_id = getattr(getattr(message, "chat", None), "id", None)
        message_id = getattr(message, "id", None)
        if chat_id is None or message_id is None:
            continue
        by_chat.setdefault(int(chat_id), []).append(int(message_id))
    if not by_chat:
        return
    factory = get_session_factory()
    try:
        async with factory() as session:
            total = 0
            for chat_id, message_ids in by_chat.items():
                total += await mark_posts_deleted(
                    session, channel_id=chat_id, message_ids=message_ids
                )
            await session.commit()
            if total:
                logger.info("Posts marked deleted count={} chats={}", total, sorted(by_chat))
    except Exception:
        logger.exception("Failed to mark deleted messages chats={}", sorted(by_chat))


async def _reload_loop(
    client: Client,
    handlers: list[Any],
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
            for old_handler in handlers:
                client.remove_handler(old_handler)
            handlers = _build_handlers(list(new_ids))
            for new_handler in handlers:
                client.add_handler(new_handler)
            last_ids = new_ids
            try:
                from app.services.heartbeat_service import touch_runtime_heartbeat

                factory = get_session_factory()
                async with factory() as session:
                    await touch_runtime_heartbeat(
                        session,
                        instance_id=os.environ.get("HOSTNAME", "tg-runtime"),
                        session_state="connected",
                        last_channel_reload_at=datetime.now(UTC),
                    )
                    await session.commit()
            except Exception:
                logger.warning("Failed to record channel reload heartbeat")
        except Exception:
            logger.exception("Failed to reload listener channel filter")


def main() -> None:
    from app.mtproto.runtime import main as runtime_main

    runtime_main()


if __name__ == "__main__":
    main()
