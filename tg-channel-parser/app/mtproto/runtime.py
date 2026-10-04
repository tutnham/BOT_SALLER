"""Single MTProto process: realtime listener + parser task worker.

Usage:
  python -m app.mtproto.runtime
"""

from __future__ import annotations

import asyncio

from loguru import logger
from pyrogram.handlers import MessageHandler

from app.config import get_settings
from app.logging_setup import setup_logging
from app.mtproto.client_factory import create_client
from app.mtproto.realtime import (
    _build_handler,
    _load_active_channel_ids,
    _reload_loop,
)
from app.worker.task_worker import run_task_poll_loop


async def run_mtproto_runtime() -> None:
    settings = get_settings()
    setup_logging(settings.log_level, settings.secret_values())
    settings.require_mtproto()
    settings.require_media_storage()

    channel_ids = await _load_active_channel_ids()
    client = create_client("runtime", settings=settings)
    handler: MessageHandler | None = _build_handler(channel_ids)
    if handler is not None:
        client.add_handler(handler)
        logger.info("Runtime listening channels={}", channel_ids)
    else:
        logger.warning(
            "No active channels — waiting for POST /channels or add_channel"
        )

    await client.start()
    logger.info("tg-runtime started (listener + task worker)")
    poll_task = asyncio.create_task(run_task_poll_loop(client))
    try:
        await _reload_loop(client, handler, settings)
    finally:
        poll_task.cancel()
        try:
            await poll_task
        except asyncio.CancelledError:
            pass
        await client.stop()
        from app.db.session import dispose_engine

        await dispose_engine()


def main() -> None:
    try:
        asyncio.run(run_mtproto_runtime())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
