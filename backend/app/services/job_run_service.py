"""Persist cron outcomes for ops metrics and /health/details."""

from __future__ import annotations

from datetime import UTC, datetime

from loguru import logger

from app.db.models import JobRun
from app.db.session import get_scheduler_session_factory


async def record_job_run(
    job_name: str,
    status: str,
    started: datetime,
    *,
    error_type: str | None = None,
) -> None:
    try:
        factory = get_scheduler_session_factory()
        async with factory() as session:
            session.add(
                JobRun(
                    job_name=job_name,
                    status=status,
                    started_at=started,
                    finished_at=datetime.now(UTC),
                    error_type=error_type,
                )
            )
            await session.commit()
    except Exception:
        logger.exception("Job {} result was not recorded", job_name)
