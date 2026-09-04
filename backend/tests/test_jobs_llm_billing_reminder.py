"""HTTP endpoint POST /jobs/llm-billing-reminder."""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import Owner
from tests.conftest import WEBHOOK_SECRET, MockTelegramClient


@pytest.mark.asyncio
async def test_llm_billing_reminder_job_endpoint(
    webhook_client: AsyncClient,
    db_session: AsyncSession,
    mock_telegram: MockTelegramClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "llm_provider_name", "DeepSeek")
    monkeypatch.setattr(settings, "llm_topup_payment_url", "https://platform.deepseek.com/top_up")
    monkeypatch.setattr(settings, "llm_topup_price_text", "500 CNY")
    monkeypatch.setattr(settings, "admin_alert_chat_id", 400400400)

    owner = Owner(telegram_id=100100100, name="Owner", dm_ok=True)
    db_session.add(owner)
    await db_session.flush()

    response = await webhook_client.post(
        "/jobs/llm-billing-reminder",
        headers={"X-Webhook-Secret": WEBHOOK_SECRET},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["sent"] == 1
    assert mock_telegram.sent[0][0] == 100100100


@pytest.mark.asyncio
async def test_llm_billing_reminder_job_endpoint_unauthorized(
    webhook_client: AsyncClient,
) -> None:
    response = await webhook_client.post("/jobs/llm-billing-reminder")
    assert response.status_code == 401
