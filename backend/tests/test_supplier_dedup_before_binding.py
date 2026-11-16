"""Duplicate supplier updates exit before any binding/AI work."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.db.models import Supplier
from app.handlers import supplier_messages as handler


def _supplier() -> Supplier:
    return Supplier(id=1, name="Supplier", active=True, telegram_id=10)


def _message() -> dict[str, object]:
    return {
        "message_id": 777,
        "from": {"id": 10},
        "chat": {"id": 10, "type": "private"},
        "text": "iPhone 17 Pro 256 Blue 117900",
    }


@pytest.mark.asyncio
async def test_duplicate_update_skips_binding_and_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _supplier_lookup(*args: object, **kwargs: object) -> Supplier:
        del args, kwargs
        return _supplier()

    async def _no_bind_session(*args: object, **kwargs: object) -> bool:
        del args, kwargs
        return False

    async def _no_reply_request(*args: object, **kwargs: object) -> None:
        del args, kwargs
        return None

    insert_calls = 0

    async def _duplicate_insert(*args: object, **kwargs: object) -> None:
        nonlocal insert_calls
        del args, kwargs
        insert_calls += 1
        return None  # unique-index conflict: already processed

    async def _boom_binding(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("resolve_binding called for a duplicate update")

    monkeypatch.setattr(handler, "get_supplier_by_telegram_id", _supplier_lookup)
    monkeypatch.setattr(handler, "try_handle_supplier_bind_session_reply", _no_bind_session)
    monkeypatch.setattr(handler, "_resolve_request_from_outbound_reply", _no_reply_request)
    monkeypatch.setattr(handler, "insert_message_in_idempotent", _duplicate_insert)
    monkeypatch.setattr(handler, "resolve_binding", _boom_binding)

    status = await handler.handle_reply(
        AsyncMock(),
        _message(),
        telegram=AsyncMock(),
        business_connection_id=None,
    )
    assert status == "ok"
    assert insert_calls == 1


@pytest.mark.asyncio
async def test_dedup_insert_precedes_resolve_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ordering guard: the claim insert must happen before the binding cascade."""
    order: list[str] = []

    async def _supplier_lookup(*args: object, **kwargs: object) -> Supplier:
        del args, kwargs
        return _supplier()

    async def _no_bind_session(*args: object, **kwargs: object) -> bool:
        del args, kwargs
        return False

    async def _no_reply_request(*args: object, **kwargs: object) -> None:
        del args, kwargs
        return None

    async def _insert(*args: object, **kwargs: object) -> None:
        del args, kwargs
        order.append("insert")
        return None

    async def _binding(*args: object, **kwargs: object) -> None:
        del args, kwargs
        order.append("binding")
        raise AssertionError("binding reached after duplicate insert")

    monkeypatch.setattr(handler, "get_supplier_by_telegram_id", _supplier_lookup)
    monkeypatch.setattr(handler, "try_handle_supplier_bind_session_reply", _no_bind_session)
    monkeypatch.setattr(handler, "_resolve_request_from_outbound_reply", _no_reply_request)
    monkeypatch.setattr(handler, "insert_message_in_idempotent", _insert)
    monkeypatch.setattr(handler, "resolve_binding", _binding)

    status = await handler.handle_reply(
        AsyncMock(),
        _message(),
        telegram=AsyncMock(),
        business_connection_id=None,
    )
    assert status == "ok"
    assert order == ["insert"]