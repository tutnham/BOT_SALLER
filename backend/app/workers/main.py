"""Coolify worker: webhook inbox + telegram outbox (no Redis)."""

from __future__ import annotations

import asyncio
import signal

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings
from app.services.inbox_service import process_inbox_batch
from app.services.outbox_service import process_outbox_batch


async def _run_loop(session_factory: async_sessionmaker[AsyncSession]) -> None:
    settings = get_settings()
    poll = settings.worker_poll_interval_seconds
    worker_id = settings.worker_id or "backend-worker"
    logger.info(
        "Worker started id={} poll={}s inbox_batch={} outbox_batch={}",
        worker_id,
        poll,
        settings.worker_inbox_batch_size,
        settings.worker_outbox_batch_size,
    )
    while True:
        async with session_factory() as session:
            try:
                inbox_n = await process_inbox_batch(session, worker_id=worker_id)
                outbox_n = await process_outbox_batch(session, worker_id=worker_id)
                await session.commit()
                if inbox_n or outbox_n:
                    logger.info(
                        "Worker tick inbox={} outbox_sent={}",
                        inbox_n,
                        outbox_n,
                    )
            except Exception:
                await session.rollback()
                logger.exception("Worker tick failed")
        await asyncio.sleep(poll)


def main() -> None:
    settings = get_settings()
    engine = create_async_engine(
        settings.database_url,
        pool_size=settings.db_scheduler_pool_size,
        max_overflow=settings.db_scheduler_max_overflow,
        pool_timeout=settings.db_scheduler_pool_timeout,
        pool_recycle=settings.db_pool_recycle,
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, loop.stop)
        except NotImplementedError:
            pass

    try:
        loop.run_until_complete(_run_loop(session_factory))
    finally:
        loop.run_until_complete(engine.dispose())
        loop.close()


if __name__ == "__main__":
    main()
