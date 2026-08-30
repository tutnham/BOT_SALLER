"""FloodWait-aware retry helper (doc §14.3)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

from loguru import logger

T = TypeVar("T")


async def with_flood_wait(
    coro_factory: Callable[[], Awaitable[T]],
    *,
    max_retries: int = 5,
) -> T:
    """
    Run async op; on FloodWait sleep e.value (or e.x) and retry.
    Other exceptions propagate.
    """
    last_exc: BaseException | None = None
    for attempt in range(1, max_retries + 1):
        try:
            return await coro_factory()
        except Exception as exc:
            wait = _flood_wait_seconds(exc)
            if wait is None:
                raise
            last_exc = exc
            logger.warning(
                "FloodWait {}s (attempt {}/{})",
                wait,
                attempt,
                max_retries,
            )
            await asyncio.sleep(wait)
    assert last_exc is not None
    raise last_exc


def _flood_wait_seconds(exc: BaseException) -> int | None:
    name = type(exc).__name__
    if "FloodWait" not in name:
        return None
    for attr in ("value", "x"):
        val = getattr(exc, attr, None)
        if isinstance(val, (int, float)):
            return int(val)
    return None
