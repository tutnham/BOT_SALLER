"""C5: версия селекции берётся под advisory lock на заявку."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.services.first_price_publication_service import _next_selection_version


@pytest.mark.asyncio
async def test_next_selection_version_takes_advisory_lock() -> None:
    executed: list[tuple[str, Any]] = []

    async def execute(stmt: Any, params: Any = None) -> None:
        executed.append((str(stmt), params))

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=execute)
    session.scalar = AsyncMock(return_value=2)

    version = await _next_selection_version(session, request_id=77)

    assert version == 3
    assert executed, "ожидался запрос advisory lock"
    lock_sql, lock_params = executed[0]
    assert "pg_advisory_xact_lock" in lock_sql
    assert "CAST(hashtext(:lock_key) AS bigint)" in lock_sql
    assert lock_params == {"lock_key": "price_selection_version:77"}


@pytest.mark.asyncio
async def test_next_selection_version_starts_at_one() -> None:
    session = AsyncMock()
    session.execute = AsyncMock()
    session.scalar = AsyncMock(return_value=None)

    assert await _next_selection_version(session, request_id=1) == 1