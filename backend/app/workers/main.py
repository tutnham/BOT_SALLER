"""Coolify worker: webhook inbox + telegram outbox (no Redis)."""

from __future__ import annotations

import asyncio
import signal

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings
from app.services.inbox_service import process_inbox_batch
from app.services.outbox_service import process_outbox_batch


async def _tick(
    session_factory: async_sessionmaker[AsyncSession],
    worker_id: str,
) -> None:
    async with session_factory() as session:
        try:
            inbox_n = await process_inbox_batch(
                session,
                worker_id=worker_id,
                session_factory=session_factory,
            )
            outbox_n = await process_outbox_batch(
                session,
                worker_id=worker_id,
                session_factory=session_factory,
            )
            if inbox_n or outbox_n:
                logger.info(
                    "Worker tick inbox={} outbox_sent={}",
                    inbox_n,
                    outbox_n,
                )
        except Exception:
            await session.rollback()
            logger.exception("Worker tick failed")


async def _run_loop(
    session_factory: async_sessionmaker[AsyncSession],
    stop: asyncio.Event,
) -> None:
    settings = get_settings()
    poll = settings.worker_poll_interval_seconds
    grace = settings.worker_shutdown_grace_seconds
    worker_id = settings.worker_id or "backend-worker"
    logger.info(
        "Worker started id={} poll={}s grace={}s inbox_batch={} outbox_batch={}",
        worker_id,
        poll,
        grace,
        settings.worker_inbox_batch_size,
        settings.worker_outbox_batch_size,
    )
    while not stop.is_set():
        tick = asyncio.create_task(_tick(session_factory, worker_id))
        waiter = asyncio.create_task(stop.wait())
        await asyncio.wait({tick, waiter}, return_when=asyncio.FIRST_COMPLETED)
        if tick.done():
            waiter.cancel()
            await tick
        else:
            try:
                await asyncio.wait_for(asyncio.shield(tick), timeout=grace)
            except TimeoutError:
                tick.cancel()
                try:
                    await tick
                except asyncio.CancelledError:
                    logger.error(
                        "Worker shutdown grace {}s exceeded; lease left for reclaim",
                        grace,
                    )
            break
        if stop.is_set():
            break
        try:
            await asyncio.wait_for(stop.wait(), timeout=poll)
        except TimeoutError:
            continue


async def _main() -> None:
    settings = get_settings()
    engine = create_async_engine(
        settings.database_url,
        pool_size=settings.db_scheduler_pool_size,
        max_overflow=settings.db_scheduler_max_overflow,
        pool_timeout=settings.db_scheduler_pool_timeout,
        pool_recycle=settings.db_pool_recycle,
        connect_args={"server_settings": {"application_name": "zakupki-worker"}},
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    try:
        await _run_loop(session_factory, stop)
    finally:
        await engine.dispose()


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
