"""Distributed PostgreSQL advisory locks for cron jobs.

Uses 64-bit signed keys derived from stable job names so scheduler, manual
/jobs endpoints, and any future replicas coordinate without extra infra.
"""

from __future__ import annotations

import hashlib
from typing import cast

from loguru import logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


def _advisory_lock_key(name: str) -> int:
    """Return a signed 64-bit bigint key from ``name``.

    PostgreSQL advisory locks accept two int4 keys or one int8 key. We use
    the first 8 bytes of SHA-256 to keep keys deterministic and well spread.
    """
    digest = hashlib.sha256(name.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


async def acquire_job_lock(session: AsyncSession, name: str) -> bool:
    """Try to acquire an advisory lock for ``name``. Returns True on success."""
    key = _advisory_lock_key(name)
    result = await session.execute(
        text("SELECT pg_try_advisory_lock(:key)").bindparams(key=key)
    )
    locked = cast(bool, result.scalar_one())
    if not locked:
        logger.info("Job lock already held: name={}", name)
    return locked


async def release_job_lock(session: AsyncSession, name: str) -> bool:
    """Release a previously acquired advisory lock. Returns True if released."""
    key = _advisory_lock_key(name)
    result = await session.execute(
        text("SELECT pg_advisory_unlock(:key)").bindparams(key=key)
    )
    released = cast(bool, result.scalar_one())
    if not released:
        logger.warning("Job lock was not held on release: name={}", name)
    return released
