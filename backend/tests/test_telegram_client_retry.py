from __future__ import annotations

import json

import httpx
import pytest

from app.telegram.client import TelegramClient, TelegramSendError


@pytest.mark.asyncio
async def test_telegram_client_retries_after_429() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                status_code=429,
                json={"ok": False, "parameters": {"retry_after": 0}, "description": "retry"},
            )
        return httpx.Response(
            status_code=200,
            json={"ok": True, "result": {"message_id": 12345}},
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = TelegramClient(bot_token="token", http_client=http_client)
        message_id = await client.send_message(100, "hello")
    assert message_id == 12345
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_telegram_client_raises_on_invalid_json_error_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=500,
            content=json.dumps("bad"),
            headers={"content-type": "application/json"},
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = TelegramClient(bot_token="token", http_client=http_client)
        with pytest.raises(TelegramSendError):
            await client.send_message(100, "hello")
