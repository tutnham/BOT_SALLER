"""In-process APScheduler setup for periodic backend jobs."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, NamedTuple
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.session import get_scheduler_session_factory
from app.jobs import (
    run_daily_report,
    run_llm_billing_reminder,
    run_morning_price,
    run_recheck_due,
)
from app.services.alert_service import send_admin_alert
from app.telegram.client import TelegramClientProtocol, get_telegram_client
from app.utils.job_lock import acquire_job_lock, release_job_lock


class JobSpec(NamedTuple):
    job_id: str
    cron: str
    name: str
    timeout_seconds: float
    alert_on_failure: bool
    misfire_grace_time: int


# Timeouts are safety nets vs hung I/O; morning-price allows parser+LLM headroom.
# misfire_grace_time is per-job: monthly reminder can tolerate a longer outage.
# morning-price at 11:00 MSK: suppliers post in price channels 08:00–11:00;
# realtime listener already writes parser_posts; this job pulls them into zakupki.
def build_job_specs(settings: Settings) -> tuple[JobSpec, ...]:
    return (
        JobSpec("morning_price", "0 11 * * *", "morning-price", 300.0, True, 300),
        JobSpec("recheck_due", "*/15 * * * *", "recheck-due", 120.0, False, 300),
        JobSpec("daily_report_day", "0 21 * * *", "daily-report-day", 60.0, False, 300),
        JobSpec("weekly_report", "0 9 * * 1", "weekly-report", 60.0, False, 300),
        JobSpec(
            "llm_billing_reminder",
            f"0 9 {settings.llm_billing_reminder_day} * *",
            "llm-billing-reminder",
            60.0,
            True,
            3600,
        ),
    )


async def _run_guarded(
    job_name: str,
    runner: Callable[[AsyncSession, TelegramClientProtocol], Awaitable[Any]],
    *,
    timeout_seconds: float,
    alert_on_failure: bool,
) -> None:
    logger.info("Job {} started", job_name)
    session_factory = get_scheduler_session_factory()
    telegram = get_telegram_client()
    async with session_factory() as session:
        if not await acquire_job_lock(session, job_name):
            logger.info("Job {} skipped: already running on another instance", job_name)
            return

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
            err_type = type(exc).__name__
            logger.exception("Job {} failed: {}", job_name, err_type)
            if alert_on_failure:
                await send_admin_alert(
                    (
                        f"⚠️ Job {job_name} failed\n"
                        f"Error: {err_type}\n"
                        "Check backend logs and parser availability."
                    ),
                    telegram=telegram,
                )
        finally:
            try:
                await release_job_lock(session, job_name)
            except Exception:
                logger.exception("Job {} advisory unlock failed", job_name)


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


async def job_llm_billing_reminder() -> None:
    await _run_guarded(
        "llm-billing-reminder",
        run_llm_billing_reminder,
        timeout_seconds=60.0,
        alert_on_failure=True,
    )


_JOB_CALLABLES: dict[str, Callable[[], Awaitable[None]]] = {
    "morning_price": job_morning_price,
    "recheck_due": job_recheck_due,
    "daily_report_day": job_daily_report_day,
    "weekly_report": job_weekly_report,
    "llm_billing_reminder": job_llm_billing_reminder,
}


def setup_scheduler() -> AsyncIOScheduler:
    settings = get_settings()
    tz = ZoneInfo(settings.tz)
    scheduler = AsyncIOScheduler(timezone=tz)
    defaults = {"max_instances": 1, "coalesce": True}

    for spec in build_job_specs(settings):
        scheduler.add_job(
            _JOB_CALLABLES[spec.job_id],
            CronTrigger.from_crontab(spec.cron, timezone=tz),
            id=spec.job_id,
            misfire_grace_time=spec.misfire_grace_time,
            **defaults,
        )
    return scheduler
