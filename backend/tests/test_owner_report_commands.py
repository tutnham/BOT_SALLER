from __future__ import annotations

import pytest
from httpx import AsyncClient


def _owner_update(*, update_id: int, from_id: int, text: str) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id * 10,
            "from": {"id": from_id, "first_name": "Owner"},
            "chat": {"id": from_id, "type": "private"},
            "text": text,
        },
    }


@pytest.mark.asyncio
async def test_owner_report_day_returns_text(
    webhook_client: AsyncClient,
    mock_telegram,
    seed_owner,
) -> None:
    mock_telegram.sent.clear()
    payload = _owner_update(update_id=52001, from_id=seed_owner.telegram_id, text="/report day")
    resp = await webhook_client.post(
        "/telegram/webhook",
        json=payload,
        headers={"X-Telegram-Bot-Api-Secret-Token": "test-telegram-webhook-secret"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert mock_telegram.sent
    assert mock_telegram.sent[-1][0] == seed_owner.telegram_id
    assert mock_telegram.parse_modes[-1] is None


@pytest.mark.asyncio
async def test_stats_week_is_report_week_synonym(
    webhook_client: AsyncClient,
    mock_telegram,
    seed_owner,
) -> None:
    mock_telegram.sent.clear()
    payload = _owner_update(update_id=52002, from_id=seed_owner.telegram_id, text="/stats week")
    resp = await webhook_client.post(
        "/telegram/webhook",
        json=payload,
        headers={"X-Telegram-Bot-Api-Secret-Token": "test-telegram-webhook-secret"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert mock_telegram.parse_modes[-1] is None


@pytest.mark.asyncio
async def test_non_owner_private_report_ignored(
    webhook_client: AsyncClient,
) -> None:
    payload = _owner_update(update_id=52003, from_id=999000111, text="/report day")
    resp = await webhook_client.post(
        "/telegram/webhook",
        json=payload,
        headers={"X-Telegram-Bot-Api-Secret-Token": "test-telegram-webhook-secret"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"
