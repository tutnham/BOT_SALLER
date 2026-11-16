"""Jev circuit breaker transitions and classifier integration. No live API calls."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from app.config import Settings, get_settings
from app.db.models import Request, RequestStatus, Supplier
from app.integrations.jev.client import set_jev_client
from app.integrations.jev.models import JevCandidate, JevProviderResponse
from app.services import jev_breaker_service as breaker
from app.services.jev_supplier_classifier import classify_with_jev


def _settings(**overrides: object) -> Settings:
    payload: dict[str, object] = {
        "telegram_bot_token": "tok",
        "database_url": "postgresql+asyncpg://u:p@localhost/db",
        "webhook_secret": "sec",
        "telegram_webhook_secret_token": "sec-telegram",
        "metrics_token": "metrics",
        "jev_enabled": True,
        "jev_api_key": "k",
        "jev_breaker_failure_streak": 3,
        "jev_breaker_cooldown_seconds": 60.0,
    }
    payload.update(overrides)
    return Settings(_env_file=None, **payload)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _silence_alerts(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    alert = AsyncMock()
    monkeypatch.setattr(breaker, "_alert", alert)
    return alert


@pytest.mark.asyncio
async def test_closed_allows_requests() -> None:
    settings = _settings()
    assert breaker.allow_jev_request(settings) is True
    assert breaker.jev_breaker_snapshot()["state"] == "closed"


@pytest.mark.asyncio
async def test_opens_after_failure_streak(_silence_alerts: AsyncMock) -> None:
    settings = _settings()
    await breaker.note_jev_breaker_failure(settings)
    await breaker.note_jev_breaker_failure(settings)
    assert breaker.allow_jev_request(settings) is True
    await breaker.note_jev_breaker_failure(settings)
    assert breaker.jev_breaker_snapshot()["state"] == "open"
    assert breaker.allow_jev_request(settings) is False
    _silence_alerts.assert_awaited_once()


@pytest.mark.asyncio
async def test_success_resets_failure_streak() -> None:
    settings = _settings()
    await breaker.note_jev_breaker_failure(settings)
    await breaker.note_jev_breaker_failure(settings)
    await breaker.note_jev_breaker_success(settings)
    await breaker.note_jev_breaker_failure(settings)
    assert breaker.jev_breaker_snapshot()["state"] == "closed"
    assert breaker.allow_jev_request(settings) is True


@pytest.mark.asyncio
async def test_cooldown_lets_exactly_one_probe_through() -> None:
    settings = _settings(jev_breaker_cooldown_seconds=0.01)
    for _ in range(3):
        await breaker.note_jev_breaker_failure(settings)
    assert breaker.allow_jev_request(settings) is False
    await asyncio.sleep(0.02)
    assert breaker.allow_jev_request(settings) is True
    assert breaker.jev_breaker_snapshot()["state"] == "half_open"
    # Everyone else fails closed while the probe is in flight.
    assert breaker.allow_jev_request(settings) is False


@pytest.mark.asyncio
async def test_probe_success_closes_breaker(_silence_alerts: AsyncMock) -> None:
    settings = _settings(jev_breaker_cooldown_seconds=0.01)
    for _ in range(3):
        await breaker.note_jev_breaker_failure(settings)
    await asyncio.sleep(0.02)
    assert breaker.allow_jev_request(settings) is True
    await breaker.note_jev_breaker_success(settings)
    assert breaker.jev_breaker_snapshot()["state"] == "closed"
    assert breaker.allow_jev_request(settings) is True
    assert _silence_alerts.await_count == 2  # open + recovered


@pytest.mark.asyncio
async def test_probe_failure_reopens_breaker() -> None:
    settings = _settings(jev_breaker_cooldown_seconds=0.01)
    for _ in range(3):
        await breaker.note_jev_breaker_failure(settings)
    await asyncio.sleep(0.02)
    assert breaker.allow_jev_request(settings) is True
    await breaker.note_jev_breaker_failure(settings)
    assert breaker.jev_breaker_snapshot()["state"] == "open"
    assert breaker.allow_jev_request(settings) is False


class _Boom:
    async def classify_supplier_message(
        self,
        *,
        message_text: str,
        candidates: list[JevCandidate],
        correlation_id: str,
    ) -> JevProviderResponse:
        del message_text, candidates, correlation_id
        raise AssertionError("provider called while breaker open")


def _supplier() -> Supplier:
    return Supplier(id=1, name="Supplier", active=True, telegram_id=10)


def _request() -> Request:
    return Request(
        id=1,
        status=RequestStatus.awaiting_answers,
        source_text="iPhone 17 Pro 256",
        normalized_json={"model": "iPhone 17 Pro", "storage": "256"},
        group_chat_id=1,
        employee_id=1,
    )


@pytest.mark.asyncio
async def test_open_breaker_falls_back_without_provider_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JEV_ENABLED", "true")
    monkeypatch.setenv("JEV_API_KEY", "k")
    monkeypatch.setenv("JEV_BREAKER_FAILURE_STREAK", "1")
    get_settings.cache_clear()

    async def _no_cache(*args: object, **kwargs: object) -> None:
        del args, kwargs
        return None

    async def _no_persist(*args: object, **kwargs: object) -> None:
        del args, kwargs
        return None

    async def _no_outbound(*args: object, **kwargs: object) -> dict[int, object]:
        del args, kwargs
        return {}

    monkeypatch.setattr(
        "app.services.jev_supplier_classifier.lookup_cached_decision", _no_cache
    )
    monkeypatch.setattr(
        "app.services.jev_supplier_classifier.persist_jev_decision", _no_persist
    )
    monkeypatch.setattr(
        "app.services.jev_supplier_classifier.load_jev_outbound_facts", _no_outbound
    )
    set_jev_client(_Boom())
    try:
        await breaker.note_jev_breaker_failure(get_settings())
        decision = await classify_with_jev(
            AsyncMock(),
            supplier=_supplier(),
            business_connection_id=None,
            chat_id=1,
            message_id=1,
            message_text="117900",
            candidates=[_request()],
            message_in_id=5,
        )
        assert decision.reason_code == "jev_breaker_open"
        # error_code keeps breaker fallbacks out of the decision cache.
        assert decision.error_code == "jev_breaker_open"
        assert decision.action.value == "fallback"
        assert decision.price_probability == Decimal("0")
    finally:
        set_jev_client(None)
        get_settings.cache_clear()