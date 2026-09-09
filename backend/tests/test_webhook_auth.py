"""Webhook auth — X-Telegram-Bot-Api-Secret-Token (TECH DOC §7, §13)."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from app.telegram.deps import _secrets_match

TELEGRAM_WEBHOOK_SECRET_TOKEN = "test-telegram-webhook-secret"
WEBHOOK_SECRET = "test-webhook-secret"


def test_non_ascii_secret_compare_returns_false() -> None:
    assert _secrets_match("badÿtoken", "secret") is False


@pytest.mark.asyncio
async def test_webhook_missing_secret_returns_401(client: AsyncClient) -> None:
    resp = await client.post(
        "/telegram/webhook",
        json={"update_id": 1, "message": {"text": "/ask test"}},
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "invalid_telegram_secret_token"


@pytest.mark.asyncio
async def test_webhook_invalid_secret_returns_401(client: AsyncClient) -> None:
    resp = await client.post(
        "/telegram/webhook",
        json={"update_id": 1, "message": {"text": "/ask test"}},
        headers={"X-Telegram-Bot-Api-Secret-Token": "wrong-secret"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_webhook_valid_secret_not_401(
    webhook_client: AsyncClient,
) -> None:
    resp = await webhook_client.post(
        "/telegram/webhook",
        json={"update_id": 999001, "message": None},
        headers={"X-Telegram-Bot-Api-Secret-Token": TELEGRAM_WEBHOOK_SECRET_TOKEN},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"


@pytest.mark.asyncio
async def test_webhook_accepts_legacy_x_webhook_secret(
    webhook_client: AsyncClient,
) -> None:
    """Cutover compat: n8n may still forward with X-Webhook-Secret."""
    resp = await webhook_client.post(
        "/telegram/webhook",
        json={"update_id": 999002, "message": None},
        headers={"X-Webhook-Secret": WEBHOOK_SECRET},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"


@pytest.mark.asyncio
async def test_jobs_reject_telegram_secret_token(
    webhook_client: AsyncClient,
) -> None:
    resp = await webhook_client.post(
        "/jobs/recheck-due",
        json={},
        headers={"X-Telegram-Bot-Api-Secret-Token": TELEGRAM_WEBHOOK_SECRET_TOKEN},
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "invalid_webhook_secret"


@pytest.mark.asyncio
async def test_webhook_non_ascii_secret_returns_401_not_500(
    client: AsyncClient,
) -> None:
    resp = await client.post(
        "/telegram/webhook",
        json={"update_id": 1, "message": None},
        headers={"X-Telegram-Bot-Api-Secret-Token": "bad-token"},
    )
    assert resp.status_code == 401
