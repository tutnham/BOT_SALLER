"""In-process APScheduler setup for periodic backend jobs."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.session import get_session_factory
from app.jobs import run_daily_report, run_morning_price, run_recheck_due
from app.services.alert_service import send_admin_alert
from app.telegram.client import TelegramClientProtocol, get_telegram_client

# (job_id, cron, runner_factory_name, timeout_seconds, alert_on_failure)
# Timeouts are safety nets vs hung I/O; morning-price allows parser+LLM headroom.
JOB_SPECS: tuple[tuple[str, str, str, float, bool], ...] = (
    ("morning_price", "0 8 * * *", "morning-price", 300.0, True),
    ("recheck_due", "*/15 * * * *", "recheck-due", 120.0, False),
    ("daily_report_day", "0 21 * * *", "daily-report-day", 60.0, False),
    ("weekly_report", "0 9 * * 1", "weekly-report", 60.0, False),
)


async def _run_guarded(
    job_name: str,
    runner: Callable[[AsyncSession, TelegramClientProtocol], Awaitable[Any]],
    *,
    timeout_seconds: float,
    alert_on_failure: bool,
) -> None:
    logger.info("Job {} started", job_name)
    session_factory = get_session_factory()
    telegram = get_telegram_client()
    async with session_factory() as session:
        try:
            await asyncio.wait_for(
                runner(session, telegram),
                timeout=timeout_seconds,
            )
            logger.info("Job {} finished successfully", job_name)
        except Exception as exc:
            try:
                await session.rollback()
            except Exception:
                logger.exception("Job {} rollback failed after error", job_name)
            err_text = str(exc).strip() or "(no message)"
            logger.exception("Job {} failed: {}: {}", job_name, type(exc).__name__, err_text)
            if alert_on_failure:
                await send_admin_alert(
                    (
                        f"⚠️ Job {job_name} failed\n"
                        f"Error: {type(exc).__name__}: {err_text}\n"
                        "Check backend logs and parser availability."
                    ),
                    telegram=telegram,
                )


async def job_morning_price() -> None:
    await _run_guarded(
        "morning-price",
        run_morning_price,
        timeout_seconds=300.0,
        alert_on_failure=True,
    )


async def job_recheck_due() -> None:
    await _run_guarded(
        "recheck-due",
        run_recheck_due,
        timeout_seconds=120.0,
        alert_on_failure=False,
    )


async def job_daily_report_day() -> None:
    await _run_guarded(
        "daily-report-day",
        lambda session, telegram: run_daily_report(
            session,
            telegram,
            period="day",
        ),
        timeout_seconds=60.0,
        alert_on_failure=False,
    )


async def job_weekly_report() -> None:
    await _run_guarded(
        "weekly-report",
        lambda session, telegram: run_daily_report(
            session,
            telegram,
            period="week",
        ),
        timeout_seconds=60.0,
        alert_on_failure=False,
    )


_JOB_CALLABLES: dict[str, Callable[[], Awaitable[None]]] = {
    "morning_price": job_morning_price,
    "recheck_due": job_recheck_due,
    "daily_report_day": job_daily_report_day,
    "weekly_report": job_weekly_report,
}


def setup_scheduler() -> AsyncIOScheduler:
    settings = get_settings()
    tz = ZoneInfo(settings.tz)
    scheduler = AsyncIOScheduler(timezone=tz)
    defaults = {"max_instances": 1, "coalesce": True, "misfire_grace_time": 300}

    for job_id, cron, _name, _timeout, _alert in JOB_SPECS:
        scheduler.add_job(
            _JOB_CALLABLES[job_id],
            CronTrigger.from_crontab(cron, timezone=tz),
            id=job_id,
            **defaults,
        )
    return scheduler
