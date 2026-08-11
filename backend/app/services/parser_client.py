"""HTTP client for tg-channel-parser microservice (TECH DOC §9.5.1)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx
from loguru import logger
from pydantic import AliasChoices, BaseModel, Field, ValidationError

from app.config import get_settings

_client: httpx.AsyncClient | None = None


def get_parser_http_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=get_settings().parser_timeout_seconds)
    return _client


class ParserClientError(Exception):
    """Parser API unavailable, auth failure, or invalid response."""


class ParserPost(BaseModel):
    """Single post from GET /posts (envelope-tolerant field names)."""

    post_id: int = Field(validation_alias=AliasChoices("post_id", "id"))
    message_id: int | None = None
    post_date: datetime
    raw_text: str | None = None
    message_link: str | None = None


def _extract_post_list(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("items", "posts", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    raise ParserClientError("invalid_posts_envelope")


async def get_posts(
    *,
    channel_id: int,
    from_: datetime | None,
    content_type: str = "text",
    http_client: httpx.AsyncClient | None = None,
) -> list[ParserPost]:
    """
    Fetch text posts for a supplier channel from the parser service.

    Raises ``ParserClientError`` on transport/HTTP/validation failures.
    """
    settings = get_settings()
    base = (settings.parser_api_url or "").rstrip("/")
    if not base:
        raise ParserClientError("parser_api_url_missing")

    params: dict[str, str] = {
        "channel_id": str(channel_id),
        "content_type": content_type,
    }
    if from_ is not None:
        params["from"] = from_.isoformat()

    headers: dict[str, str] = {}
    token = settings.parser_api_token
    if token:
        headers["Authorization"] = f"Bearer {token}"

    owns_client = False
    client = http_client or get_parser_http_client()
    try:
        response: httpx.Response | None = None
        for attempt in range(1, 4):
            try:
                response = await client.get(
                    f"{base}/posts",
                    params=params,
                    headers=headers,
                )
            except httpx.HTTPError as exc:
                if attempt == 3:
                    raise ParserClientError(f"parser_transport_error:{exc}") from exc
                continue
            if response.status_code < 500:
                break

        assert response is not None
        if response.status_code >= 400:
            raise ParserClientError(f"parser_http_{response.status_code}")

        if len(response.content) > 5_000_000:
            raise ParserClientError("parser_response_too_large")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ParserClientError("parser_invalid_json") from exc

        raw_items = _extract_post_list(payload)
        posts: list[ParserPost] = []
        for item in raw_items:
            if not isinstance(item, dict):
                logger.warning("Skipping non-object parser post for channel_id={}", channel_id)
                continue
            try:
                posts.append(ParserPost.model_validate(item))
            except ValidationError:
                logger.warning(
                    "Skipping invalid parser post for channel_id={}",
                    channel_id,
                )
        return posts
    finally:
        if owns_client:
            await client.aclose()
