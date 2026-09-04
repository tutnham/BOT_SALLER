"""Cron job endpoints for manual /jobs/* execution."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.jobs import (
    run_daily_report,
    run_llm_billing_reminder,
    run_morning_price,
    run_recheck_due,
)
from app.telegram.client import get_telegram_client
from app.telegram.deps import verify_webhook_secret
from app.utils.job_lock import acquire_job_lock, release_job_lock

router = APIRouter(prefix="/jobs", tags=["jobs"])


class DailyReportPayload(BaseModel):
    period: Literal["day", "week"]


async def _run_locked(
    session: AsyncSession,
    lock_name: str,
    runner: Any,
) -> Any:
    """Run a job under a distributed advisory lock; skip if already running."""
    if not await acquire_job_lock(session, lock_name):
        return {"status": "skipped", "reason": "already_running"}
    try:
        return await runner(session, get_telegram_client())
    finally:
        await release_job_lock(session, lock_name)


@router.post("/daily-report", dependencies=[Depends(verify_webhook_secret)])
async def daily_report_job(
    payload: DailyReportPayload,
    session: AsyncSession = Depends(get_db),
) -> dict[str, int | str]:
    return await _run_locked(
        session,
        f"daily-report-{payload.period}",
        lambda session, telegram: run_daily_report(
            session,
            telegram,
            period=payload.period,
        ),
    )


@router.post("/morning-price", dependencies=[Depends(verify_webhook_secret)])
async def morning_price_job(
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Cron entry for §9.5 morning price pipeline. Always 200 when handled."""
    return await _run_locked(
        session,
        "morning-price",
        run_morning_price,
    )


@router.post("/recheck-due", dependencies=[Depends(verify_webhook_secret)])
async def recheck_due_job(
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Cron entry for §9.4 recheck poller. Always 200 when handled."""
    return await _run_locked(
        session,
        "recheck-due",
        run_recheck_due,
    )


@router.post("/llm-billing-reminder", dependencies=[Depends(verify_webhook_secret)])
async def llm_billing_reminder_job(
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Cron entry for monthly LLM top-up reminder. Always 200 when handled."""
    return await _run_locked(
        session,
        "llm-billing-reminder",
        run_llm_billing_reminder,
    )
