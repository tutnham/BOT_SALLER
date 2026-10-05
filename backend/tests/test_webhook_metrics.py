"""Webhook and /metrics instrumentation."""

from __future__ import annotations

from prometheus_client import CollectorRegistry, generate_latest

import pytest

from app.services.telemetry import configure_registry, get_metrics, record_webhook_accepted


@pytest.fixture
def metrics_registry() -> CollectorRegistry:
    return configure_registry(CollectorRegistry())


def _counter_value(registry: CollectorRegistry, name: str) -> float:
    text = generate_latest(registry).decode("utf-8")
    for line in text.splitlines():
        if line.startswith(name + " "):
            return float(line.split()[-1])
    return 0.0


@pytest.mark.asyncio
async def test_webhook_success_increments_total_once(client, metrics_registry) -> None:
    before = _counter_value(metrics_registry, "zakupki_webhook_total")
    response = await client.post(
        "/telegram/webhook",
        json={"update_id": 880001, "message": {"message_id": 1, "chat": {"id": 1}}},
        headers={"X-Telegram-Bot-Api-Secret-Token": "test-telegram-webhook-secret"},
    )
    assert response.status_code == 200
    after = _counter_value(metrics_registry, "zakupki_webhook_total")
    assert after - before == 1.0


@pytest.mark.asyncio
async def test_duplicate_webhook_still_counts_accepted_request(
    client, db_session, metrics_registry
) -> None:
    from app.utils.idempotency import mark_update_processed

    await mark_update_processed(db_session, 880002)
    await db_session.commit()
    before = _counter_value(metrics_registry, "zakupki_webhook_total")
    response = await client.post(
        "/telegram/webhook",
        json={"update_id": 880002, "message": {"message_id": 2, "chat": {"id": 1}}},
        headers={"X-Telegram-Bot-Api-Secret-Token": "test-telegram-webhook-secret"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "duplicate"
    after = _counter_value(metrics_registry, "zakupki_webhook_total")
    assert after - before == 1.0


@pytest.mark.asyncio
async def test_metrics_requires_token(client) -> None:
    denied = await client.get("/metrics")
    assert denied.status_code == 401
    ok = await client.get("/metrics", headers={"X-Metrics-Token": "test-metrics-token"})
    assert ok.status_code == 200
    assert "zakupki_webhook_total" in ok.text
    assert "test-metrics-token" not in ok.text
