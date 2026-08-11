"""Telegram Bot API client — HTTP only, no MTProto (TECH DOC §5, §6)."""

from __future__ import annotations

import asyncio
import random
from typing import Any, Protocol

import httpx
from loguru import logger

from app.config import get_settings
from app.telegram.throttle import throttle_send

TELEGRAM_API_BASE = "https://api.telegram.org"


class TelegramSendError(Exception):
    """Raised when Telegram Bot API returns non-ok response."""

    def __init__(self, description: str, *, status_code: int | None = None) -> None:
        super().__init__(description)
        self.description = description
        self.status_code = status_code


class TelegramClientProtocol(Protocol):
    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        parse_mode: str | None = None,
    ) -> int:
        """Send text message; return ``message_id``."""
        ...


class TelegramClient:
    """Async wrapper around ``sendMessage`` Bot API method."""

    def __init__(
        self,
        *,
        bot_token: str | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        settings = get_settings()
        self._bot_token = bot_token or settings.telegram_bot_token
        self._owns_client = http_client is None
        self._http = http_client or httpx.AsyncClient(timeout=30.0)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._http.aclose()

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        parse_mode: str | None = None,
    ) -> int:
        url = f"{TELEGRAM_API_BASE}/bot{self._bot_token}/sendMessage"
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if parse_mode is not None:
            payload["parse_mode"] = parse_mode
        await throttle_send(chat_id)
        max_attempts = 4
        for attempt in range(1, max_attempts + 1):
            try:
                response = await self._http.post(url, json=payload)
            except httpx.HTTPError as exc:
                if attempt >= max_attempts:
                    raise TelegramSendError(
                        f"transport_error:{type(exc).__name__}",
                        status_code=None,
                    ) from exc
                await asyncio.sleep(min(3.0, 0.2 * (2 ** (attempt - 1))))
                continue

            try:
                data = response.json()
            except ValueError:
                data = {"ok": False, "description": "invalid_json_response"}
            if not isinstance(data, dict):
                data = {"ok": False, "description": "invalid_json_response"}

            if response.status_code == 200 and data.get("ok"):
                result = data["result"]
                return int(result["message_id"])

            retry_after = _extract_retry_after_seconds(data)
            can_retry = response.status_code in {429, 500, 502, 503, 504}
            if can_retry and attempt < max_attempts:
                base_wait = retry_after if retry_after is not None else 0.3 * (2 ** (attempt - 1))
                await asyncio.sleep(min(8.0, base_wait + random.uniform(0.0, 0.15)))
                continue

            description = data.get("description", response.text)
            logger.warning(
                "Telegram sendMessage failed chat_id={} status={} detail={}",
                chat_id,
                response.status_code,
                description,
            )
            raise TelegramSendError(
                str(description),
                status_code=response.status_code,
            )

        raise TelegramSendError("telegram_send_failed")


def _extract_retry_after_seconds(payload: dict[str, Any]) -> float | None:
    params = payload.get("parameters")
    if not isinstance(params, dict):
        return None
    value = params.get("retry_after")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


_client: TelegramClient | TelegramClientProtocol | None = None


def get_telegram_client() -> TelegramClientProtocol:
    """Return shared Telegram client (lazy singleton)."""
    global _client
    if _client is None:
        _client = TelegramClient()
    return _client


def set_telegram_client(
    client: TelegramClient | TelegramClientProtocol | None,
) -> None:
    """Override client (tests) or reset to lazy singleton."""
    global _client
    _client = client
