"""HTTP client for tg-channel-parser microservice (TECH DOC §9.5.1)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx
from loguru import logger
from pydantic import AliasChoices, BaseModel, Field, ValidationError

from app.config import get_settings

_client: httpx.AsyncClient | None = None
DEFAULT_POSTS_PAGE_SIZE = 100
MAX_POSTS_PAGE_SIZE = 500


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


class ParserChannel(BaseModel):
    """Channel from GET /channels."""

    id: int
    channel_id: int | None = None
    username: str | None = None
    title: str | None = None
    purpose: str
    status: str = "active"
    is_active: bool = True

    model_config = {"from_attributes": True}


def _base_url() -> str:
    settings = get_settings()
    base = (settings.parser_api_url or "").rstrip("/")
    if not base:
        raise ParserClientError("parser_api_url_missing")
    return base


def _headers() -> dict[str, str]:
    token = get_settings().parser_api_token
    headers: dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


async def _request(
    method: str,
    path: str,
    *,
    params: dict[str, str] | None = None,
    json_body: dict[str, Any] | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> Any:
    """Make a parser API request with retries. Returns JSON body (dict or list)."""
    base = _base_url()
    client = http_client or get_parser_http_client()

    response: httpx.Response | None = None
    for attempt in range(1, 4):
        try:
            if method == "GET":
                response = await client.get(
                    f"{base}{path}",
                    params=params,
                    headers=_headers(),
                )
            elif method == "POST":
                response = await client.post(
                    f"{base}{path}",
                    json=json_body,
                    headers=_headers(),
                )
            elif method == "DELETE":
                response = await client.delete(
                    f"{base}{path}",
                    headers=_headers(),
                )
            else:
                raise ParserClientError(f"unsupported_method:{method}")
        except httpx.HTTPError as exc:
            if attempt == 3:
                raise ParserClientError(f"parser_transport_error:{exc}") from exc
            continue
        if response.status_code < 500:
            break

    assert response is not None
    if response.status_code >= 400:
        raise ParserClientError(f"parser_http_{response.status_code}")

    try:
        payload = response.json()
    except ValueError as exc:
        raise ParserClientError("parser_invalid_json") from exc

    if not isinstance(payload, dict | list):
        raise ParserClientError("parser_invalid_json")
    return payload


def _extract_list(payload: Any, *, keys: tuple[str, ...] = ("items", "posts", "results")) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, list):
                return value
    raise ParserClientError("invalid_list_envelope")


def _extract_posts_page(payload: Any) -> tuple[list[Any], str | None]:
    """Return raw post dicts and optional next_cursor from paginated or legacy payloads."""
    if isinstance(payload, list):
        return payload, None
    if isinstance(payload, dict):
        items = payload.get("items")
        if isinstance(items, list):
            next_cursor = payload.get("next_cursor")
            if next_cursor is not None and not isinstance(next_cursor, str):
                raise ParserClientError("invalid_posts_envelope")
            return items, next_cursor if next_cursor else None
        return _extract_list(payload), None
    raise ParserClientError("invalid_posts_envelope")


def _validate_posts(raw_items: list[Any], *, channel_id: int) -> list[ParserPost]:
    posts: list[ParserPost] = []
    for item in raw_items:
        if not isinstance(item, dict):
            logger.warning("Skipping non-object parser post for channel_id={}", channel_id)
            continue
        try:
            posts.append(ParserPost.model_validate(item))
        except ValidationError:
            logger.warning("Skipping invalid parser post for channel_id={}", channel_id)
    return posts


async def get_posts(
    *,
    channel_id: int,
    from_: datetime | None,
    content_type: str = "text",
    http_client: httpx.AsyncClient | None = None,
) -> list[ParserPost]:
    """Fetch text posts for a supplier channel from the parser service."""
    all_posts: list[ParserPost] = []
    cursor: str | None = None

    while True:
        params: dict[str, str] = {
            "channel_id": str(channel_id),
            "content_type": content_type,
            "limit": str(DEFAULT_POSTS_PAGE_SIZE),
        }
        if from_ is not None:
            params["from"] = from_.isoformat()
        if cursor is not None:
            params["cursor"] = cursor

        payload = await _request(
            "GET",
            "/posts",
            params=params,
            http_client=http_client,
        )
        raw_items, next_cursor = _extract_posts_page(payload)
        all_posts.extend(_validate_posts(raw_items, channel_id=channel_id))
        if not next_cursor:
            break
        cursor = next_cursor

    return all_posts


async def list_channels(
    http_client: httpx.AsyncClient | None = None,
) -> list[ParserChannel]:
    """Return all registered parser channels."""
    payload = await _request("GET", "/channels", http_client=http_client)
    raw_items = _extract_list(payload)
    channels: list[ParserChannel] = []
    for item in raw_items:
        if not isinstance(item, dict):
            logger.warning("Skipping non-object parser channel")
            continue
        try:
            channels.append(ParserChannel.model_validate(item))
        except ValidationError:
            logger.warning("Skipping invalid parser channel")
    return channels


async def add_channel(
    handle: str,
    *,
    purpose: str = "supplier_price_source",
    http_client: httpx.AsyncClient | None = None,
) -> ParserChannel:
    """Register a channel in the parser; it will be resolved asynchronously."""
    payload = await _request(
        "POST",
        "/channels",
        json_body={"handle": handle, "purpose": purpose},
        http_client=http_client,
    )
    if not isinstance(payload, dict):
        raise ParserClientError("parser_invalid_channel_response")
    try:
        return ParserChannel.model_validate(payload)
    except ValidationError as exc:
        raise ParserClientError(f"parser_invalid_channel_response:{exc}") from exc


async def delete_channel(
    channel_id: int,
    *,
    http_client: httpx.AsyncClient | None = None,
) -> None:
    """Deactivate a parser channel."""
    await _request("DELETE", f"/channels/{channel_id}", http_client=http_client)
