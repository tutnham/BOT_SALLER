"""Production compose must not run cron in both web and scheduler services."""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE = REPO_ROOT / "docker-compose.yml"


def test_backend_web_scheduler_disabled_and_expected() -> None:
    text = COMPOSE.read_text(encoding="utf-8")
    backend_block = text.split("backend-worker:")[0]
    assert 'SCHEDULER_ENABLED: "false"' in backend_block
    assert 'SCHEDULER_EXPECTED: "true"' in backend_block


def test_dedicated_scheduler_command() -> None:
    text = COMPOSE.read_text(encoding="utf-8")
    assert "backend-scheduler:" in text
    scheduler_block = text.split("backend-scheduler:")[1].split("networks:")[0]
    assert 'python", "-m", "app.scheduler"' in scheduler_block.replace("\n", " ")
    assert 'SCHEDULER_ENABLED: "false"' in scheduler_block


def test_worker_scheduler_disabled() -> None:
    text = COMPOSE.read_text(encoding="utf-8")
    worker_block = text.split("backend-worker:")[1].split("backend-scheduler:")[0]
    assert 'SCHEDULER_ENABLED: "false"' in worker_block
