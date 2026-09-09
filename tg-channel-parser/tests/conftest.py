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
        link=link,
    )


@pytest_asyncio.fixture
async def client() -> AsyncGenerator[AsyncClient, None]:
    """ASGI client with stubbed DB dependency (no real Postgres)."""
    from app.api.main import app

    posts_store: list[Any] = []
    channels_store: list[Any] = []

    class _Result:
        def __init__(self, rows: list[Any]) -> None:
            self._rows = rows

        def scalars(self) -> SimpleNamespace:
            return SimpleNamespace(all=lambda: list(self._rows))

        def scalar_one_or_none(self) -> Any:
            return self._rows[0] if self._rows else None

    class FakeSession:
        def __init__(self) -> None:
            self.posts = posts_store
            self.channels = channels_store

        async def execute(self, stmt: Any) -> _Result:
            sql = str(stmt)
            if "parser_channels" in sql.lower() or "ParserChannel" in sql:
                return _Result(self.channels)
            if "parser_posts" in sql.lower() or "ParserPost" in sql:
                rows = list(self.posts)
                return _Result(rows)
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
        yield ac

    app.dependency_overrides.clear()
    get_settings.cache_clear()
