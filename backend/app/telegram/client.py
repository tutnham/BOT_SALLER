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

MAX_ATTEMPTS = 4


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
        reply_markup: dict[str, Any] | None = None,
    ) -> int:
        """Send text message; return ``message_id``."""
        ...

    async def answer_callback_query(
        self,
        callback_query_id: str,
        *,
        text: str | None = None,
        show_alert: bool = False,
    ) -> None:
        """Acknowledge callback query."""
        ...

    async def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        parse_mode: str | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        """Edit existing message text."""
        ...

    async def get_chat(self, chat_id: int) -> dict[str, Any] | None:
        """Return chat info or None if not accessible."""
        ...


class TelegramClient:
    """Async wrapper around Bot API methods used by Zakupki-Bot."""

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

    async def _request(
        self,
        method: str,
        payload: dict[str, Any],
        *,
        throttle_chat_id: int | None = None,
        success_ok: bool = True,
        return_result: bool = True,
    ) -> dict[str, Any] | None:
        """Post to Bot API with retries and flood-wait backoff.

        ``throttle_chat_id`` is used for sendMessage throttling; pure
        methods like answerCallbackQuery can pass None to skip it.
        """
        url = f"{TELEGRAM_API_BASE}/bot{self._bot_token}/{method}"
        if throttle_chat_id is not None:
            await throttle_send(throttle_chat_id)

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = await self._http.post(url, json=payload)
            except httpx.HTTPError as exc:
                if attempt >= MAX_ATTEMPTS:
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
                if return_result:
                    return data.get("result") or {}
                return data

            retry_after = _extract_retry_after_seconds(data)
            can_retry = response.status_code in {429, 500, 502, 503, 504}
            if can_retry and attempt < MAX_ATTEMPTS:
                base_wait = retry_after if retry_after is not None else 0.3 * (2 ** (attempt - 1))
                await asyncio.sleep(min(8.0, base_wait + random.uniform(0.0, 0.15)))
                continue

            description = data.get("description", response.text)
            logger.warning(
                "Telegram {} failed chat_id={} status={} detail={}",
                method,
                payload.get("chat_id"),
                response.status_code,
                description,
            )
            raise TelegramSendError(str(description), status_code=response.status_code)

        raise TelegramSendError("telegram_request_failed")

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        parse_mode: str | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> int:
        """Send text message; return ``message_id``."""
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if parse_mode is not None:
            payload["parse_mode"] = parse_mode
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        result = await self._request("sendMessage", payload, throttle_chat_id=chat_id)
        if not isinstance(result, dict) or "message_id" not in result:
            raise TelegramSendError("telegram_empty_send_result")
        return int(result["message_id"])

    async def answer_callback_query(
        self,
        callback_query_id: str,
        *,
        text: str | None = None,
        show_alert: bool = False,
    ) -> None:
        """Acknowledge callback query to stop the loading spinner."""
        payload: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text is not None:
            payload["text"] = text
        if show_alert:
            payload["show_alert"] = True
        await self._request(
            "answerCallbackQuery",
            payload,
            throttle_chat_id=None,
            return_result=False,
        )

    async def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        parse_mode: str | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        """Edit existing message text."""
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
        }
        if parse_mode is not None:
            payload["parse_mode"] = parse_mode
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        await self._request(
            "editMessageText",
            payload,
            throttle_chat_id=None,
            return_result=False,
        )

    async def get_chat(self, chat_id: int) -> dict[str, Any] | None:
        """Return chat info or None if not accessible / not found."""
        try:
            result = await self._request(
                "getChat",
                {"chat_id": chat_id},
                throttle_chat_id=None,
            )
        except TelegramSendError as exc:
            if exc.status_code == 400 or "chat not found" in str(exc.description).lower():
                return None
            raise
        return result if isinstance(result, dict) else None


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
