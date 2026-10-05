"""Upsert process heartbeats used by /ready."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import ProcessHeartbeat


async def touch_heartbeat(
    session: AsyncSession,
    *,
    process_type: str,
    instance_id: str,
) -> None:
    settings = get_settings()
    now = datetime.now(UTC)
    stmt = insert(ProcessHeartbeat).values(
        process_type=process_type,
        instance_id=instance_id,
        started_at=now,
        heartbeat_at=now,
        app_version=settings.app_version,
        commit_sha=settings.commit_sha,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["process_type", "instance_id"],
        set_={
            "heartbeat_at": now,
            "app_version": settings.app_version,
            "commit_sha": settings.commit_sha,
        },
    )
    await session.execute(stmt)


async def latest_heartbeat_at(
    session: AsyncSession,
    process_type: str,
) -> datetime | None:
    value = (
        await session.execute(
            select(func.max(ProcessHeartbeat.heartbeat_at)).where(
                ProcessHeartbeat.process_type == process_type
            )
        )
    ).scalar_one_or_none()
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def heartbeat_is_fresh(heartbeat_at: datetime | None, *, now: datetime | None = None) -> bool:
    if heartbeat_at is None:
        return False
    current = now or datetime.now(UTC)
    stale = timedelta(seconds=get_settings().heartbeat_stale_seconds)
    return current - heartbeat_at <= stale
