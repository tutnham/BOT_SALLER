"""Tests for GET /posts contract and pagination."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from httpx import AsyncClient
from pydantic import AliasChoices, BaseModel, Field, ValidationError

from app.db.models import ParserContentType
from tests.conftest import API_AUTH_TOKEN


class ParserPost(BaseModel):
    """Mirror of backend/app/services/parser_client.py ParserPost."""

    post_id: int = Field(validation_alias=AliasChoices("post_id", "id"))
    message_id: int | None = None
    post_date: datetime
    raw_text: str | None = None
    message_link: str | None = None


@pytest.mark.asyncio
async def test_posts_contract_validates_parser_post(client: AsyncClient) -> None:
    client.posts_store.clear()  # type: ignore[attr-defined]
    client.posts_store.append(  # type: ignore[attr-defined]
        SimpleNamespace(
            id=11,
            channel_id=-1001,
            message_id=4711,
            post_date=datetime(2026, 7, 30, 6, 0, tzinfo=timezone.utc),
            content_type=ParserContentType.text,
            raw_text="iPhone 15 70000",
            message_link="https://t.me/c/1/11",
        )
    )

    resp = await client.get(
        "/posts",
        params={"channel_id": -1001, "content_type": "text"},
        headers={"Authorization": f"Bearer {API_AUTH_TOKEN}"},
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert isinstance(payload, dict)
    assert isinstance(payload["items"], list)
    assert len(payload["items"]) == 1
    assert payload["next_cursor"] is None

    parsed = ParserPost.model_validate(payload["items"][0])
    assert parsed.post_id == 11
    assert parsed.message_id == 4711
    assert parsed.raw_text == "iPhone 15 70000"
    assert parsed.message_link == "https://t.me/c/1/11"


@pytest.mark.asyncio
async def test_posts_pagination_returns_next_cursor(client: AsyncClient) -> None:
    client.posts_store.clear()  # type: ignore[attr-defined]
    for idx in range(3):
        client.posts_store.append(  # type: ignore[attr-defined]
            SimpleNamespace(
                id=idx + 1,
                channel_id=-1001,
                message_id=100 + idx,
                post_date=datetime(2026, 7, 30, 6, idx, tzinfo=timezone.utc),
                content_type=ParserContentType.text,
                raw_text=f"price {idx}",
                message_link=f"https://t.me/c/1/{100 + idx}",
            )
        )

    first = await client.get(
        "/posts",
        params={"channel_id": -1001, "content_type": "text", "limit": "2"},
        headers={"Authorization": f"Bearer {API_AUTH_TOKEN}"},
    )
    assert first.status_code == 200
    first_payload = first.json()
    assert len(first_payload["items"]) == 2
    assert first_payload["next_cursor"]

    second = await client.get(
        "/posts",
        params={
            "channel_id": -1001,
            "content_type": "text",
            "limit": "2",
            "cursor": first_payload["next_cursor"],
        },
        headers={"Authorization": f"Bearer {API_AUTH_TOKEN}"},
    )
    assert second.status_code == 200
    second_payload = second.json()
    assert len(second_payload["items"]) == 1
    assert second_payload["next_cursor"] is None


@pytest.mark.asyncio
async def test_posts_contract_rejects_broken_shape() -> None:
    with pytest.raises(ValidationError):
        ParserPost.model_validate({"raw_text": "no id/date"})
