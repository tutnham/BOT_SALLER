"""Single MTProto process: realtime listener + parser task worker.

Usage:
  python -m app.mtproto.runtime
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from typing import Any

from loguru import logger
from pyrogram.handlers import MessageHandler

from app.config import get_settings
from app.db.session import get_session_factory
from app.logging_setup import setup_logging
from app.mtproto.client_factory import create_client
from app.mtproto.realtime import (
    _build_handler,
    _load_active_channel_ids,
    _reload_loop,
)
from app.services.heartbeat_service import touch_runtime_heartbeat
from app.worker.task_worker import run_task_poll_loop


async def _runtime_heartbeat_loop(client: Any, instance_id: str) -> None:
    started = datetime.now(UTC)
    factory = get_session_factory()
    while True:
        state = "connected" if client.is_connected else "disconnected"
        try:
            async with factory() as session:
                await touch_runtime_heartbeat(
                    session,
                    instance_id=instance_id,
                    session_state=state,
                    started_at=started,
                )
                await session.commit()
        except Exception:
            logger.warning("Runtime heartbeat write failed")
        await asyncio.sleep(15)


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
    instance_id = os.environ.get("HOSTNAME", "tg-runtime")
    logger.info("tg-runtime started (listener + task worker)")
    poll_task = asyncio.create_task(run_task_poll_loop(client))
    heartbeat_task = asyncio.create_task(_runtime_heartbeat_loop(client, instance_id))
    try:
        await _reload_loop(client, handler, settings)
    finally:
        heartbeat_task.cancel()
        poll_task.cancel()
        for task in (heartbeat_task, poll_task):
            try:
                await task
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
