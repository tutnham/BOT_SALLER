"""Tests for listener empty-state hot reload."""

from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest


def _install_pyrogram_stub() -> None:
    if "pyrogram" in sys.modules:
        return
    pyrogram = ModuleType("pyrogram")
    pyrogram.Client = MagicMock()
    pyrogram.filters = MagicMock()
    pyrogram.handlers = ModuleType("pyrogram.handlers")
    pyrogram.handlers.MessageHandler = MagicMock()
    sys.modules["pyrogram"] = pyrogram
    sys.modules["pyrogram.filters"] = pyrogram.filters
    sys.modules["pyrogram.handlers"] = pyrogram.handlers


_install_pyrogram_stub()

from app.mtproto import realtime  # noqa: E402


@pytest.mark.asyncio
async def test_build_handler_returns_none_for_empty_channels() -> None:
    with patch.object(realtime, "MessageHandler", MagicMock()):
        assert realtime._build_handler([]) is None


@pytest.mark.asyncio
async def test_build_handler_returns_handler_for_active_channels() -> None:
    handler = MagicMock()
    with patch.object(realtime, "MessageHandler", MagicMock(return_value=handler)):
        built = realtime._build_handler([-100555])
    assert built is handler
