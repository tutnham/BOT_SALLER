"""Scheduler process entrypoint: module order, heartbeat, SIGTERM."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import ProcessHeartbeat
from app.scheduler import build_job_specs, setup_scheduler

BACKEND_ROOT = Path(__file__).resolve().parents[1]


def test_scheduler_module_entrypoint_defines_setup_before_main() -> None:
    source = (BACKEND_ROOT / "app" / "scheduler.py").read_text(encoding="utf-8")
    setup_pos = source.index("def setup_scheduler(")
    main_pos = source.index("def main() -> None:")
    entry_pos = source.index('if __name__ == "__main__":')
    assert setup_pos < main_pos < entry_pos
    assert source.strip().endswith("main()")


def test_setup_scheduler_job_ids_match_specs() -> None:
    specs = build_job_specs(__import__("app.config", fromlist=["get_settings"]).get_settings())
    scheduler = setup_scheduler()
    assert {job.id for job in scheduler.get_jobs()} == {spec.job_id for spec in specs}


@pytest.mark.skipif(sys.platform == "win32", reason="SIGTERM scheduler test runs on Linux CI")
@pytest.mark.asyncio
async def test_scheduler_process_writes_heartbeat_and_sigterm_clean(
    engine,
    test_database_url: str,
) -> None:
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    env = os.environ.copy()
    env.update(
        {
            "DATABASE_URL": test_database_url,
            "TELEGRAM_BOT_TOKEN": "test-bot-token",
            "WEBHOOK_SECRET": "test-webhook-secret",
            "TELEGRAM_WEBHOOK_SECRET_TOKEN": "test-telegram-webhook-secret",
            "SCHEDULER_ENABLED": "false",
            "HOSTNAME": "pytest-scheduler",
        }
    )
    proc = await asyncio.to_thread(
        subprocess.Popen,
        [sys.executable, "-m", "app.scheduler"],
        cwd=BACKEND_ROOT,
        env=env,
    )
    try:
        await _wait_scheduler_heartbeat(session_factory, "pytest-scheduler", timeout=30.0)
        proc.send_signal(signal.SIGTERM)
        exit_code = await asyncio.to_thread(proc.wait, 30)
        assert exit_code == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


async def _wait_scheduler_heartbeat(
    session_factory: async_sessionmaker,
    instance_id: str,
    *,
    timeout: float,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        async with session_factory() as session:
            row = await session.scalar(
                select(ProcessHeartbeat).where(
                    ProcessHeartbeat.process_type == "scheduler",
                    ProcessHeartbeat.instance_id == instance_id,
                )
            )
            if row is not None:
                return
        await asyncio.sleep(0.5)
    raise AssertionError("scheduler heartbeat not written in time")
