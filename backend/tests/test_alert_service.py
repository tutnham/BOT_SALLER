"""Direct tests for admin alert helper."""

from __future__ import annotations

import pytest

from app.config import get_settings
from app.services.alert_service import send_admin_alert
from app.telegram.client import TelegramSendError


class _MockTelegram:
    def __init__(self, *, raise_on: int | None = None) -> None:
        self.sent: list[tuple[int, str]] = []
        self.raise_on = raise_on

    async def send_message(self, chat_id: int, text: str, **kwargs: object) -> int:
        if self.raise_on is not None and chat_id == self.raise_on:
            raise TelegramSendError("network")
        self.sent.append((chat_id, text))
        return 1


@pytest.mark.asyncio
async def test_alert_sends_to_admin_chat_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "admin_alert_chat_id", 400400400)
    telegram = _MockTelegram()

    await send_admin_alert("boom", telegram=telegram)

    assert telegram.sent == [(400400400, "boom")]


@pytest.mark.asyncio
async def test_alert_no_op_when_chat_id_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "admin_alert_chat_id", None)
    telegram = _MockTelegram()

    await send_admin_alert("boom", telegram=telegram)

    assert telegram.sent == []


@pytest.mark.asyncio
async def test_alert_survives_telegram_send_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "admin_alert_chat_id", 400400400)
    telegram = _MockTelegram(raise_on=400400400)

    await send_admin_alert("boom", telegram=telegram)

    assert telegram.sent == []
