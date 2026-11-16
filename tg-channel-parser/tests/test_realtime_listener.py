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
    pyrogram.handlers.EditedMessageHandler = MagicMock()
    pyrogram.handlers.DeletedMessagesHandler = MagicMock()
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


@pytest.mark.asyncio
async def test_build_handlers_empty_channels_returns_empty_list() -> None:
    assert realtime._build_handlers([]) == []


@pytest.mark.asyncio
async def test_build_handlers_registers_new_edit_delete() -> None:
    # B1: листнер должен слушать новые посты, редактирования и удаления
    with (
        patch.object(realtime, "MessageHandler", MagicMock()) as message_handler,
        patch.object(realtime, "EditedMessageHandler", MagicMock()) as edit_handler,
        patch.object(realtime, "DeletedMessagesHandler", MagicMock()) as delete_handler,
    ):
        handlers = realtime._build_handlers([-100555])
    assert len(handlers) == 3
    assert message_handler.call_count == 1
    assert edit_handler.call_count == 1
    assert delete_handler.call_count == 1
    assert edit_handler.call_args.args[0] is realtime._on_edited_post
    assert delete_handler.call_args.args[0] is realtime._on_deleted_messages
