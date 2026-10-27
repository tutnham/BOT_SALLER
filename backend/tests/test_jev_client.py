"""Mocked Jev HTTP adapter. No live API calls."""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from app.config import Settings
from app.integrations.jev.client import HttpJevClient
from app.integrations.jev.exceptions import (
    JevAuthError,
    JevInvalidResponse,
    JevRateLimited,
    JevTimeout,
    JevUnavailable,
)
from app.integrations.jev.models import JevCandidate


def _settings(**overrides: object) -> Settings:
    payload: dict[str, object] = {
        "telegram_bot_token": "tok",
        "database_url": "postgresql+asyncpg://u:p@localhost/db",
        "webhook_secret": "sec",
        "telegram_webhook_secret_token": "sec-telegram",
        "metrics_token": "metrics",
        "jev_enabled": True,
        "jev_api_key": "super-secret-jev-key",
        "jev_api_base_url": "https://api.typesafe.ai",
        "jev_model": "jev-1.14.0",
        "jev_timeout_seconds": 1,
        "jev_max_retries": 0,
    }
    payload.update(overrides)
    return Settings(_env_file=None, **payload)  # type: ignore[arg-type]


def _candidate() -> JevCandidate:
    return JevCandidate(candidate_id="request:7", description="iPhone 17 Pro 256 Blue")


def _ok_body(choice: str = "request:7") -> dict[str, object]:
    return {
        "model": "jev-1.14.0",
        "answers": {
            "message_type": {
                "type": "choice",
                "choice": "price_offer",
                "probabilities": {"price_offer": 0.99, "not_price": 0.01, "ambiguous": 0.0},
                "confidence": 0.97,
            },
            "is_price": {"type": "noul", "noul": 0.99},
            "binding": {
                "type": "choice",
                "choice": choice,
                "probabilities": {choice: 0.98, "none": 0.01, "ambiguous": 0.01},
                "confidence": 0.96,
            },
        },
        "usage": {"input_tokens": 20, "output_tokens": 0},
    }


def _client(handler: object, **overrides: object) -> HttpJevClient:
    return HttpJevClient(
        settings=_settings(**overrides),
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_successful_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/systemone"
        assert request.headers["authorization"] == "Bearer super-secret-jev-key"
        return httpx.Response(200, json=_ok_body())

    client = _client(handler)
    result = await client.classify_supplier_message(
        message_text="117900",
        candidates=[_candidate()],
        correlation_id="c1",
    )
    await client.aclose()
    assert result.model == "jev-1.14.0"
    assert result.binding_choice == "request:7"
    assert result.price_probability == Decimal("0.99")


@pytest.mark.asyncio
async def test_authentication_error_redacts_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "bad super-secret-jev-key"})

    client = _client(handler)
    with pytest.raises(JevAuthError) as caught:
        await client.classify_supplier_message(
            message_text="117900",
            candidates=[_candidate()],
            correlation_id="c1",
        )
    await client.aclose()
    assert "super-secret-jev-key" not in str(caught.value)


@pytest.mark.asyncio
async def test_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out super-secret-jev-key")

    client = _client(handler)
    with pytest.raises(JevTimeout) as caught:
        await client.classify_supplier_message(
            message_text="117900",
            candidates=[_candidate()],
            correlation_id="c1",
        )
    await client.aclose()
    assert "super-secret-jev-key" not in str(caught.value)


@pytest.mark.asyncio
async def test_rate_limit() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"detail": "slow down"})

    client = _client(handler)
    with pytest.raises(JevRateLimited):
        await client.classify_supplier_message(
            message_text="117900",
            candidates=[_candidate()],
            correlation_id="c1",
        )
    await client.aclose()


@pytest.mark.asyncio
async def test_server_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "down"})

    client = _client(handler)
    with pytest.raises(JevUnavailable):
        await client.classify_supplier_message(
            message_text="117900",
            candidates=[_candidate()],
            correlation_id="c1",
        )
    await client.aclose()


@pytest.mark.asyncio
async def test_invalid_json() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"{", headers={"content-type": "application/json"})

    client = _client(handler)
    with pytest.raises(JevInvalidResponse):
        await client.classify_supplier_message(
            message_text="117900",
            candidates=[_candidate()],
            correlation_id="c1",
        )
    await client.aclose()


@pytest.mark.asyncio
async def test_unexpected_schema() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"model": "jev-1.14.0", "answers": {}})

    client = _client(handler)
    with pytest.raises(JevInvalidResponse):
        await client.classify_supplier_message(
            message_text="117900",
            candidates=[_candidate()],
            correlation_id="c1",
        )
    await client.aclose()


@pytest.mark.asyncio
async def test_unknown_candidate_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ok_body("request:999"))

    client = _client(handler)
    with pytest.raises(JevInvalidResponse):
        await client.classify_supplier_message(
            message_text="117900",
            candidates=[_candidate()],
            correlation_id="c1",
        )
    await client.aclose()


@pytest.mark.asyncio
async def test_oversized_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b'{"model":"x"}' + b" " * 300_000,
            headers={"content-type": "application/json"},
        )

    client = _client(handler)
    with pytest.raises(JevInvalidResponse) as caught:
        await client.classify_supplier_message(
            message_text="117900",
            candidates=[_candidate()],
            correlation_id="c1",
        )
    await client.aclose()
    assert caught.value.code == "jev_response_too_large"
