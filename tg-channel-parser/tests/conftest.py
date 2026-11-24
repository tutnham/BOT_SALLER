"""Pytest fixtures for tg-channel-parser."""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

# Required settings before importing app
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://tg_parser:changeme@127.0.0.1:5433/tg_parser",
)
os.environ["API_AUTH_TOKEN"] = "test-parser-api-token"
os.environ.setdefault("TELEGRAM_API_ID", "12345")
os.environ.setdefault("TELEGRAM_API_HASH", "0123456789abcdef0123456789abcdef")
os.environ.setdefault("TELEGRAM_SESSION_STRING", "test-session-string-not-real")
os.environ.setdefault("LOG_LEVEL", "WARNING")

from app.config import get_settings  # noqa: E402
from app.db.session import get_db  # noqa: E402

get_settings.cache_clear()

API_AUTH_TOKEN = "test-parser-api-token"


def _emulate_posts_select(stmt: Any, rows: list[Any]) -> list[Any]:
    """Эмуляция WHERE/ORDER BY/LIMIT select-запроса для стаб-сессии (B5).

    Prod-код не содержит тестовых срезов: курсор и лимит исполняет SQL,
    а стаб-сессия воспроизводит их поведение на Python-объектах.
    """
    from sqlalchemy.sql import operators
    from sqlalchemy.sql.elements import BinaryExpression, BindParameter, Tuple

    def _bind_value(node: Any) -> Any:
        return node.value if isinstance(node, BindParameter) else node

    result = list(rows)
    whereclause = getattr(stmt, "whereclause", None)
    criteria = (
        getattr(whereclause, "clauses", [whereclause])
        if whereclause is not None
        else []
    )
    for crit in criteria:
        if not isinstance(crit, BinaryExpression):
            continue
        left, right, op = crit.left, crit.right, crit.operator
        if isinstance(left, Tuple) and isinstance(right, Tuple):
            # курсор: (post_date, id) > (:date, :id)
            keys = [getattr(col, "key", None) for col in left.clauses]
            values = tuple(_bind_value(v) for v in right.clauses)
            if op is operators.gt and all(keys):
                result = [
                    row
                    for row in result
                    if tuple(getattr(row, k, None) for k in keys) > values
                ]
            continue
        key = getattr(left, "key", None)
        if key is None:
            continue
        value = _bind_value(right)
        if op is operators.eq:
            result = [row for row in result if getattr(row, key, None) == value]
        elif op is operators.ge:
            result = [
                row
                for row in result
                if getattr(row, key, None) is not None and getattr(row, key) >= value
            ]
    order_keys: list[tuple[str, bool]] = []
    for clause in getattr(stmt, "_order_by_clauses", ()) or ():
        element = getattr(clause, "element", clause)
        key = getattr(element, "key", None)
        if key:
            modifier = getattr(clause, "modifier", None)
            ascending = modifier is not operators.desc_op
            order_keys.append((key, ascending))
    for key, ascending in reversed(order_keys):
        result.sort(key=lambda row, key=key: getattr(row, key, None), reverse=not ascending)
    limit_value = getattr(getattr(stmt, "_limit_clause", None), "value", None)
    if isinstance(limit_value, int):
        result = result[:limit_value]
    return result


def make_message(
    *,
    chat_id: int = -100123,
    message_id: int = 1,
    text: str | None = "hello",
    caption: str | None = None,
    photo: Any = None,
    video: Any = None,
    document: Any = None,
    audio: Any = None,
    voice: Any = None,
    media: Any = None,
    media_group_id: int | None = None,
    date: datetime | None = None,
    edit_date: datetime | None = None,
    link: str | None = "https://t.me/c/123/1",
) -> SimpleNamespace:
    return SimpleNamespace(
        id=message_id,
        chat=SimpleNamespace(id=chat_id, username="ch", title="Channel"),
        text=text,
        caption=caption,
        photo=photo,
        video=video,
        document=document,
        audio=audio,
        voice=voice,
        media=media,
        media_group_id=media_group_id,
        date=date or datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        edit_date=edit_date,
        link=link,
    )


@pytest_asyncio.fixture
async def client() -> AsyncGenerator[AsyncClient, None]:
    """ASGI client with stubbed DB dependency (no real Postgres)."""
    from app.api.main import app
    from app.db.revision import EXPECTED_ALEMBIC_REVISION

    posts_store: list[Any] = []
    channels_store: list[Any] = []
    runtime_store: list[Any] = []

    class _Result:
        def __init__(self, rows: list[Any]) -> None:
            self._rows = rows

        def scalars(self) -> SimpleNamespace:
            return SimpleNamespace(all=lambda: list(self._rows))

        def scalar_one(self) -> Any:
            return self._rows[0] if self._rows else 0

        def scalar_one_or_none(self) -> Any:
            return self._rows[0] if self._rows else None

    class FakeSession:
        def __init__(self) -> None:
            self.posts = posts_store
            self.channels = channels_store

        async def execute(self, stmt: Any) -> _Result:
            sql = str(stmt)
            sql_lower = sql.lower()
            if "alembic_version" in sql_lower:
                return _Result([EXPECTED_ALEMBIC_REVISION])
            if "parser_runtime_heartbeats" in sql_lower:
                return _Result(list(runtime_store[-1:]))
            if "parser_channels" in sql_lower or "ParserChannel" in sql:
                return _Result(self.channels)
            if "parser_posts" in sql_lower or "ParserPost" in sql:
                rows = list(self.posts)
                # Эмуляция SQL-фильтра excluded IS false: стаб-сессия не исполняет WHERE
                if "excluded is false" in sql_lower:
                    rows = [row for row in rows if not getattr(row, "excluded", False)]
                rows = _emulate_posts_select(stmt, rows)
                return _Result(rows)
            if "count(" in sql_lower:
                return _Result([0])
            if "min(" in sql_lower or "max(" in sql_lower:
                return _Result([])
            return _Result([])

        async def commit(self) -> None:
            return None

        async def rollback(self) -> None:
            return None

        async def close(self) -> None:
            return None

    async def override_get_db() -> AsyncGenerator[FakeSession, None]:
        yield FakeSession()

    app.dependency_overrides[get_db] = override_get_db
    get_settings.cache_clear()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # attach stores for tests
        ac.posts_store = posts_store  # type: ignore[attr-defined]
        ac.channels_store = channels_store  # type: ignore[attr-defined]
        ac.runtime_store = runtime_store  # type: ignore[attr-defined]
        yield ac

    app.dependency_overrides.clear()
    get_settings.cache_clear()
