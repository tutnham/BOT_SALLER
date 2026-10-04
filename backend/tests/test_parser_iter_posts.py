"""Parser page iterator regression."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.services.parser_client import iter_posts


@pytest.mark.asyncio
async def test_iter_posts_yields_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    pages = [
        [{"id": 1, "post_date": "2026-01-01T00:00:00+00:00", "raw_text": "a"}],
        [{"id": 2, "post_date": "2026-01-02T00:00:00+00:00", "raw_text": "b"}],
    ]
    async def fake_request(method, path, *, params=None, json_body=None, http_client=None):
        cursor = (params or {}).get("cursor")
        if cursor is None:
            return {"items": pages[0], "next_cursor": "c1"}
        return {"items": pages[1], "next_cursor": None}

    monkeypatch.setattr("app.services.parser_client._request", fake_request)

    collected: list[int] = []
    async for page in iter_posts(
        channel_id=1,
        from_=datetime(2026, 1, 1, tzinfo=UTC),
    ):
        collected.extend(post.post_id for post in page)

    assert collected == [1, 2]
