"""B2: устойчивость LLM-клиента — JSON, retry 429/transport, закрытие httpx."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable

import httpx
import pytest

from app.config import get_settings
from app.llm.client import (
    DefaultLLMClient,
    LLMProviderError,
    close_llm_client,
    set_llm_client,
)


@pytest.fixture
def llm_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "openai_compatible")
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_MODEL", "gpt-test")
    monkeypatch.setenv("LLM_BASE_URL", "http://llm.test/v1")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def fast_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Убираем реальные задержки backoff из тестов."""

    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)


def _make_client(handler: Callable[[httpx.Request], httpx.Response]) -> DefaultLLMClient:
    client = DefaultLLMClient()
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


def _ok_response(payload: dict) -> httpx.Response:
    content = json.dumps(
        {"available": True, "price": 85000, "qty": 1, "confidence": 0.9}
    )
    return httpx.Response(
        200,
        json={"choices": [{"message": {"content": content}}], **payload},
    )


@pytest.mark.asyncio
async def test_invalid_json_body_raises_provider_error(llm_env: None) -> None:
    # HTTP 200 с невалидным JSON: должен быть LLMProviderError, чтобы сработал fallback
    client = _make_client(lambda request: httpx.Response(200, text="not-json"))
    try:
        with pytest.raises(LLMProviderError, match="llm_invalid_json"):
            await client.parse_supplier_reply("iPhone 15 85000")
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_null_message_raises_provider_error(llm_env: None) -> None:
    client = _make_client(
        lambda request: httpx.Response(200, json={"choices": [{"message": None}]})
    )
    try:
        with pytest.raises(LLMProviderError, match="llm_empty_content"):
            await client.parse_supplier_reply("iPhone 15 85000")
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_non_dict_body_raises_provider_error(llm_env: None) -> None:
    client = _make_client(lambda request: httpx.Response(200, json=["unexpected"]))
    try:
        with pytest.raises(LLMProviderError, match="llm_malformed_response"):
            await client.parse_supplier_reply("iPhone 15 85000")
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_429_is_retried_then_succeeds(llm_env: None, fast_retry: None) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return _ok_response({})

    client = _make_client(handler)
    try:
        parsed = await client.parse_supplier_reply("iPhone 15 85000")
        assert parsed.price == 85000
        assert calls["n"] == 2
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_transport_error_is_retried(llm_env: None, fast_retry: None) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ConnectError("connection reset", request=request)
        return _ok_response({})

    client = _make_client(handler)
    try:
        parsed = await client.parse_supplier_reply("iPhone 15 85000")
        assert parsed.price == 85000
        assert calls["n"] == 3
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_transport_error_exhausted_maps_to_provider_error(
    llm_env: None, fast_retry: None
) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ReadTimeout("timeout", request=request)

    client = _make_client(handler)
    try:
        with pytest.raises(LLMProviderError, match="llm_transport_error"):
            await client.parse_supplier_reply("iPhone 15 85000")
        assert calls["n"] == 3
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_5xx_still_retried(llm_env: None, fast_retry: None) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(502)
        return _ok_response({})

    client = _make_client(handler)
    try:
        parsed = await client.parse_supplier_reply("iPhone 15 85000")
        assert parsed.price == 85000
        assert calls["n"] == 3
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_close_llm_client_closes_http(llm_env: None) -> None:
    client = _make_client(lambda request: _ok_response({}))
    set_llm_client(client)
    try:
        await close_llm_client()
        assert client._http.is_closed
    finally:
        set_llm_client(None)