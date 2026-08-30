"""GET /posts contract must validate via zakupki-compatible ParserPost schema."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from httpx import AsyncClient
from pydantic import AliasChoices, BaseModel, Field, ValidationError

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
    client.posts_store.append(  # type: ignore[attr-defined]
        SimpleNamespace(
            id=11,
            channel_id=-1001,
            message_id=4711,
            post_date=datetime(2026, 7, 30, 6, 0, tzinfo=timezone.utc),
            content_type=SimpleNamespace(value="text"),  # enum-like
            raw_text="iPhone 15 70000",
            message_link="https://t.me/c/1/11",
        )
    )
    # content_type may be enum — PostOut expects str via from_attributes;
    # FastAPI will coerce Enum to value. Use real enum:
    from app.db.models import ParserContentType

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
    assert isinstance(payload, list)
    assert len(payload) == 1

    parsed = ParserPost.model_validate(payload[0])
    assert parsed.post_id == 11
    assert parsed.message_id == 4711
    assert parsed.raw_text == "iPhone 15 70000"
    assert parsed.message_link == "https://t.me/c/1/11"


@pytest.mark.asyncio
async def test_posts_contract_rejects_broken_shape() -> None:
    with pytest.raises(ValidationError):
        ParserPost.model_validate({"raw_text": "no id/date"})
