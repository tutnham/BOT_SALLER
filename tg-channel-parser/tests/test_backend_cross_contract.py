"""Cross-service contract: parser API page shape consumed by backend client logic."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from httpx import AsyncClient
from pydantic import AliasChoices, BaseModel, Field

from app.db.models import ParserContentType
from tests.conftest import API_AUTH_TOKEN


class ParserPost(BaseModel):
    post_id: int = Field(validation_alias=AliasChoices("post_id", "id"))
    message_id: int | None = None
    post_date: datetime
    raw_text: str | None = None
    message_link: str | None = None


def _extract_posts_page(payload: object) -> tuple[list[dict], str | None]:
    if isinstance(payload, dict):
        items = payload.get("items")
        if isinstance(items, list):
            next_cursor = payload.get("next_cursor")
            return items, next_cursor if isinstance(next_cursor, str) else None
    raise AssertionError("invalid_posts_envelope")


@pytest.mark.asyncio
async def test_backend_style_client_reads_parser_page(client: AsyncClient) -> None:
    client.posts_store.clear()  # type: ignore[attr-defined]
    client.posts_store.append(  # type: ignore[attr-defined]
        SimpleNamespace(
            id=99,
            channel_id=-100777,
            message_id=501,
            post_date=datetime(2026, 8, 1, 9, 0, tzinfo=UTC),
            content_type=ParserContentType.text,
            raw_text="Contract post",
            message_link="https://t.me/c/777/501",
        )
    )

    response = await client.get(
        "/posts",
        params={"channel_id": -100777, "content_type": "text"},
        headers={"Authorization": f"Bearer {API_AUTH_TOKEN}"},
    )

    assert response.status_code == 200
    raw_items, next_cursor = _extract_posts_page(response.json())
    assert next_cursor is None
    posts = [ParserPost.model_validate(item) for item in raw_items]
    assert posts[0].post_id == 99
    assert posts[0].raw_text == "Contract post"


@pytest.mark.asyncio
async def test_backend_style_client_follows_cursor(client: AsyncClient) -> None:
    client.posts_store.clear()  # type: ignore[attr-defined]
    for idx in range(2):
        client.posts_store.append(  # type: ignore[attr-defined]
            SimpleNamespace(
                id=idx + 1,
                channel_id=-100777,
                message_id=600 + idx,
                post_date=datetime(2026, 8, 1, 9, idx, tzinfo=UTC),
                content_type=ParserContentType.text,
                raw_text=f"post-{idx}",
                message_link=f"https://t.me/c/777/{600 + idx}",
            )
        )

    first = await client.get(
        "/posts",
        params={"channel_id": -100777, "content_type": "text", "limit": "1"},
        headers={"Authorization": f"Bearer {API_AUTH_TOKEN}"},
    )
    first_items, cursor = _extract_posts_page(first.json())
    assert len(first_items) == 1
    assert cursor

    second = await client.get(
        "/posts",
        params={
            "channel_id": -100777,
            "content_type": "text",
            "limit": "1",
            "cursor": cursor,
        },
        headers={"Authorization": f"Bearer {API_AUTH_TOKEN}"},
    )
    second_items, next_cursor = _extract_posts_page(second.json())

    assert ParserPost.model_validate(first_items[0]).raw_text == "post-0"
    assert ParserPost.model_validate(second_items[0]).raw_text == "post-1"
    assert next_cursor is None
