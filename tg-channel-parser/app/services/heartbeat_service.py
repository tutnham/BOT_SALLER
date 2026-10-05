"""Runtime heartbeat upsert for readiness."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import ParserRuntimeHeartbeat


async def touch_runtime_heartbeat(
    session: AsyncSession,
    *,
    instance_id: str,
    session_state: str,
    last_channel_reload_at: datetime | None = None,
    started_at: datetime | None = None,
) -> None:
    now = datetime.now(UTC)
    start = started_at or now
    values = {
        "instance_id": instance_id,
        "started_at": start,
        "heartbeat_at": now,
        "session_state": session_state,
        "last_channel_reload_at": last_channel_reload_at,
    }
    stmt = insert(ParserRuntimeHeartbeat).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["instance_id"],
        set_={
            "heartbeat_at": now,
            "session_state": session_state,
            "last_channel_reload_at": last_channel_reload_at,
        },
    )
    await session.execute(stmt)


async def latest_runtime_heartbeat_at(session: AsyncSession) -> datetime | None:
    value = (
        await session.execute(select(func.max(ParserRuntimeHeartbeat.heartbeat_at)))
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
