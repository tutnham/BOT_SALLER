"""Parser API readiness and operational details."""

from __future__ import annotations

import hmac
import os
import shutil
from datetime import UTC, datetime

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import ParserPost, ParserStatus, ParserTask
from app.db.revision import EXPECTED_ALEMBIC_REVISION
from app.services.heartbeat_service import (
    heartbeat_is_fresh,
    latest_runtime_heartbeat_at,
)


def details_token_ok(presented: str | None) -> bool:
    settings = get_settings()
    expected = settings.health_details_token or settings.api_auth_token
    if not presented or not expected:
        return False
    return hmac.compare_digest(presented, expected)


async def _alembic_revision(session: AsyncSession) -> str | None:
    try:
        return (await session.execute(text("SELECT version_num FROM alembic_version"))).scalar_one_or_none()
    except Exception:
        return None


async def readiness(session: AsyncSession) -> tuple[bool, dict[str, object]]:
    settings = get_settings()
    db_ok = True
    try:
        await session.execute(text("SELECT 1"))
    except Exception:
        db_ok = False
    revision = await _alembic_revision(session) if db_ok else None
    schema_ok = revision == EXPECTED_ALEMBIC_REVISION
    reasons: list[str] = []
    if not db_ok:
        reasons.append("database")
    if not schema_ok:
        reasons.append("schema")
    runtime_ok = True
    if settings.runtime_required:
        runtime_at = await latest_runtime_heartbeat_at(session) if db_ok else None
        runtime_ok = heartbeat_is_fresh(runtime_at)
        if not runtime_ok:
            reasons.append("runtime")
    ready = db_ok and schema_ok and runtime_ok
    return ready, {
        "status": "ok" if ready else "not_ready",
        "db": "ok" if db_ok else "error",
        "schema": revision if schema_ok else "mismatch",
        "reasons": reasons,
    }


async def health_details(session: AsyncSession) -> dict[str, object]:
    ready, ready_body = await readiness(session)
    now = datetime.now(UTC)
    pending = int(
        (
            await session.execute(
                select(func.count()).where(ParserTask.status == ParserStatus.new)
            )
        ).scalar_one()
    )
    processing = int(
        (
            await session.execute(
                select(func.count()).where(ParserTask.status == ParserStatus.processing)
            )
        ).scalar_one()
    )
    failed = int(
        (
            await session.execute(
                select(func.count()).where(ParserTask.status == ParserStatus.failed)
            )
        ).scalar_one()
    )
    oldest = (
        await session.execute(
            select(func.min(ParserTask.created_at)).where(
                ParserTask.status.in_((ParserStatus.new, ParserStatus.processing))
            )
        )
    ).scalar_one_or_none()
    oldest_age = None
    if oldest is not None:
        if oldest.tzinfo is None:
            oldest = oldest.replace(tzinfo=UTC)
        oldest_age = int((now - oldest).total_seconds())
    last_post = (
        await session.execute(select(func.max(ParserPost.post_date)))
    ).scalar_one_or_none()
    runtime_at = await latest_runtime_heartbeat_at(session)
    media_path = get_settings().media_local_path
    try:
        usage = shutil.disk_usage(media_path)
        media_status = "ok" if usage.free > 50 * 1024 * 1024 else "low_space"
    except OSError:
        media_status = "unavailable"
    return {
        "ready": ready,
        "readiness": ready_body,
        "tasks_pending": pending,
        "tasks_processing": processing,
        "tasks_failed": failed,
        "oldest_actionable_task_age_seconds": oldest_age,
        "last_post_at": last_post.isoformat() if last_post else None,
        "runtime_heartbeat_at": runtime_at.isoformat() if runtime_at else None,
        "media_volume_status": media_status,
        "instance_id": os.environ.get("HOSTNAME", "tg-runtime"),
    }
