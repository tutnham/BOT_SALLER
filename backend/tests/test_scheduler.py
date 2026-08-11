"""APScheduler wiring for in-process cron jobs."""

from __future__ import annotations

from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from app import scheduler as scheduler_mod
from app.config import get_settings
from app.scheduler import JOB_SPECS, setup_scheduler


class _DummySession:
    def __init__(self) -> None:
        self.rolled_back = False

    async def rollback(self) -> None:
        self.rolled_back = True


class _DummySessionFactory:
    def __init__(self) -> None:
        self.last_session: _DummySession | None = None

    def __call__(self) -> _DummySessionFactory:
        return self

    async def __aenter__(self) -> _DummySession:
        self.last_session = _DummySession()
        return self.last_session

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


class _DummyTelegram:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str, *, parse_mode=None) -> int:
        self.sent.append((chat_id, text))
        return 1


def test_setup_scheduler_registers_expected_jobs() -> None:
    settings = get_settings()
    tz = ZoneInfo(settings.tz)
    scheduler = setup_scheduler()

    jobs = {job.id: job for job in scheduler.get_jobs()}
    expected_ids = {spec[0] for spec in JOB_SPECS}
    assert set(jobs) == expected_ids

    assert str(jobs["morning_price"].trigger) == (
        "cron[month='*', day='*', day_of_week='*', hour='8', minute='0']"
    )
    assert str(jobs["recheck_due"].trigger) == (
        "cron[month='*', day='*', day_of_week='*', hour='*', minute='*/15']"
    )
    assert str(jobs["daily_report_day"].trigger) == (
        "cron[month='*', day='*', day_of_week='*', hour='21', minute='0']"
    )
    assert str(jobs["weekly_report"].trigger) == (
        "cron[month='*', day='*', day_of_week='1', hour='9', minute='0']"
    )
    for job in jobs.values():
        assert job.max_instances == 1
        assert job.coalesce is True
        assert job.misfire_grace_time == 300
        assert job.trigger.timezone == tz


def test_job_specs_timeouts_and_alert_flags() -> None:
    by_id = {job_id: (timeout, alert) for job_id, _c, _n, timeout, alert in JOB_SPECS}
    assert by_id["morning_price"] == (300.0, True)
    assert by_id["recheck_due"] == (120.0, False)
    assert by_id["daily_report_day"] == (60.0, False)
    assert by_id["weekly_report"] == (60.0, False)


@pytest.mark.asyncio
async def test_job_morning_price_alerts_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    session_factory = _DummySessionFactory()
    telegram = _DummyTelegram()
    alert = AsyncMock()

    monkeypatch.setattr(scheduler_mod, "get_session_factory", lambda: session_factory)
    monkeypatch.setattr(scheduler_mod, "get_telegram_client", lambda: telegram)
    monkeypatch.setattr(
        scheduler_mod,
        "run_morning_price",
        AsyncMock(side_effect=RuntimeError("boom")),
    )
    monkeypatch.setattr(scheduler_mod, "send_admin_alert", alert)

    await scheduler_mod.job_morning_price()

    assert session_factory.last_session is not None
    assert session_factory.last_session.rolled_back is True
    assert alert.await_count == 1
    assert "morning-price" in alert.await_args.args[0]


@pytest.mark.asyncio
async def test_run_guarded_alerts_after_rollback_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _BrokenRollbackSession(_DummySession):
        async def rollback(self) -> None:
            self.rolled_back = True
            raise RuntimeError("rollback broken")

    class _Factory(_DummySessionFactory):
        async def __aenter__(self) -> _BrokenRollbackSession:
            self.last_session = _BrokenRollbackSession()
            return self.last_session  # type: ignore[return-value]

    session_factory = _Factory()
    telegram = _DummyTelegram()
    alert = AsyncMock()

    monkeypatch.setattr(scheduler_mod, "get_session_factory", lambda: session_factory)
    monkeypatch.setattr(scheduler_mod, "get_telegram_client", lambda: telegram)
    monkeypatch.setattr(scheduler_mod, "send_admin_alert", alert)

    await scheduler_mod._run_guarded(
        "morning-price",
        AsyncMock(side_effect=TimeoutError()),
        timeout_seconds=1.0,
        alert_on_failure=True,
    )

    assert alert.await_count == 1
    assert "TimeoutError" in alert.await_args.args[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "job_name,runner_name",
    [
        ("recheck-due", "run_recheck_due"),
        ("daily-report-day", "run_daily_report"),
        ("weekly-report", "run_daily_report"),
    ],
)
async def test_non_alerting_jobs_do_not_notify_on_failure(
    monkeypatch: pytest.MonkeyPatch,
    job_name: str,
    runner_name: str,
) -> None:
    session_factory = _DummySessionFactory()
    telegram = _DummyTelegram()
    alert = AsyncMock()

    monkeypatch.setattr(scheduler_mod, "get_session_factory", lambda: session_factory)
    monkeypatch.setattr(scheduler_mod, "get_telegram_client", lambda: telegram)
    monkeypatch.setattr(
        scheduler_mod,
        runner_name,
        AsyncMock(side_effect=RuntimeError("boom")),
    )
    monkeypatch.setattr(scheduler_mod, "send_admin_alert", alert)

    await getattr(scheduler_mod, f"job_{job_name.replace('-', '_')}")()

    assert session_factory.last_session is not None
    assert session_factory.last_session.rolled_back is True
    assert alert.await_count == 0


@pytest.mark.asyncio
async def test_job_recheck_due_smoke(monkeypatch: pytest.MonkeyPatch) -> None:
    session_factory = _DummySessionFactory()
    telegram = _DummyTelegram()
    runner = AsyncMock(return_value={"status": "ok"})
    alert = AsyncMock()

    monkeypatch.setattr(scheduler_mod, "get_session_factory", lambda: session_factory)
    monkeypatch.setattr(scheduler_mod, "get_telegram_client", lambda: telegram)
    monkeypatch.setattr(scheduler_mod, "run_recheck_due", runner)
    monkeypatch.setattr(scheduler_mod, "send_admin_alert", alert)

    await scheduler_mod.job_recheck_due()

    runner.assert_awaited_once()
    assert alert.await_count == 0
