"""Cron job endpoints for manual /jobs/* execution."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.jobs import run_daily_report, run_morning_price, run_recheck_due
from app.telegram.client import get_telegram_client
from app.telegram.deps import verify_webhook_secret

router = APIRouter(prefix="/jobs", tags=["jobs"])


class DailyReportPayload(BaseModel):
    period: Literal["day", "week"]


@router.post("/daily-report", dependencies=[Depends(verify_webhook_secret)])
async def daily_report_job(
    payload: DailyReportPayload,
    session: AsyncSession = Depends(get_db),
) -> dict[str, int | str]:
    telegram = get_telegram_client()
    return await run_daily_report(session, telegram, period=payload.period)


@router.post("/morning-price", dependencies=[Depends(verify_webhook_secret)])
async def morning_price_job(
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Cron entry for §9.5 morning price pipeline. Always 200 when handled."""
    telegram = get_telegram_client()
    return await run_morning_price(session, telegram)


@router.post("/recheck-due", dependencies=[Depends(verify_webhook_secret)])
async def recheck_due_job(
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Cron entry for §9.4 recheck poller. Always 200 when handled."""
    telegram = get_telegram_client()
    return await run_recheck_due(session, telegram)
