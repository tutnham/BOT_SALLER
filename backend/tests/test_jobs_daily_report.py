from __future__ import annotations

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_jobs_daily_report_requires_secret(webhook_client: AsyncClient) -> None:
    resp = await webhook_client.post("/jobs/daily-report", json={"period": "day"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_jobs_daily_report_sends_report_to_owner_dm(
    webhook_client: AsyncClient,
    mock_telegram,
    seed_owner,
) -> None:
    mock_telegram.sent.clear()
    resp = await webhook_client.post(
        "/jobs/daily-report",
        json={"period": "day"},
        headers={"X-Webhook-Secret": "test-webhook-secret"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["period"] == "day"
    assert body["sent"] >= 1
    assert body["failed"] == 0
    assert any(chat_id == seed_owner.telegram_id for chat_id, _ in mock_telegram.sent)
    assert mock_telegram.parse_modes[-1] == "HTML"
