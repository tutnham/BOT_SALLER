"""HTTP client for tg-channel-parser microservice (TECH DOC §9.5.1)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

import httpx
from loguru import logger
from pydantic import AliasChoices, BaseModel, Field, ValidationError

from app.config import get_settings

_client: httpx.AsyncClient | None = None
DEFAULT_POSTS_PAGE_SIZE = 100
MAX_POSTS_PAGE_SIZE = 500
# B3: предохранитель от бесконечной пагинации (зацикленный cursor / дефект парсера)
MAX_POSTS_PAGES = 200


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
    purpose: str = "supplier_price_source"
    status: str = "active"
    is_active: bool = True
    error_text: str | None = None

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


def classify_parser_error(exc: BaseException) -> str:
    """Стабильный токен причины для алертов и экрана «Парсер».

    Пустой PARSER_API_TOKEN учитываем только на 401: иначе транспортная ошибка
    или 5xx выглядели бы как проблема секрета.
    """
    settings = get_settings()
    message = str(exc)
    if not (settings.parser_api_url or "").strip():
        return "url_missing"
    if "parser_transport_error" in message or "parser_no_response" in message:
        return "unreachable"
    if "parser_http_401" in message or "parser_http_403" in message:
        if not (settings.parser_api_token or "").strip():
            return "token_missing"
        return "auth"
    if "parser_http_" in message:
        return "http_error"
    if "invalid" in message:
        return "bad_response"
    return "unknown"


async def _request(
    method: str,
    path: str,
    *,
    params: dict[str, str] | None = None,
    json_body: dict[str, Any] | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> Any:
    """Make a parser API request with retries. Returns JSON body (dict or list)."""
    from app.services.telemetry import observe_http_async, record_parser_error

    operation = f"parser_{method.lower()}"
    base = _base_url()
    client = http_client or get_parser_http_client()

    response: httpx.Response | None = None
    async with observe_http_async(operation):
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
                    record_parser_error()
                    raise ParserClientError(f"parser_transport_error:{exc}") from exc
                await asyncio.sleep(0.2 * (2 ** (attempt - 1)))
                continue
            if response.status_code < 500:
                break
            if attempt < 3:
                await asyncio.sleep(0.2 * (2 ** (attempt - 1)))

    if response is None:
        # C6: явное исключение вместо assert (python -O)
        raise ParserClientError("parser_no_response")
    if response.status_code >= 400:
        record_parser_error()
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


async def iter_posts(
    *,
    channel_id: int,
    from_: datetime | None,
    content_type: str = "text",
    http_client: httpx.AsyncClient | None = None,
    page_size: int = DEFAULT_POSTS_PAGE_SIZE,
) -> AsyncIterator[list[ParserPost]]:
    """Stream parser posts page-by-page (bounded memory)."""
    cursor: str | None = None
    limit = min(max(page_size, 1), MAX_POSTS_PAGE_SIZE)
    seen_cursors: set[str] = set()
    pages = 0

    while True:
        pages += 1
        if pages > MAX_POSTS_PAGES:
            raise ParserClientError("parser_pagination_limit_exceeded")
        params: dict[str, str] = {
            "channel_id": str(channel_id),
            "content_type": content_type,
            "limit": str(limit),
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
        page = _validate_posts(raw_items, channel_id=channel_id)
        if page:
            yield page
        if not next_cursor:
            break
        if next_cursor in seen_cursors:
            # Парсер вернул уже виденный cursor — иначе цикл никогда не завершится
            raise ParserClientError("parser_pagination_cursor_loop")
        seen_cursors.add(next_cursor)
        cursor = next_cursor


async def get_posts(
    *,
    channel_id: int,
    from_: datetime | None,
    content_type: str = "text",
    http_client: httpx.AsyncClient | None = None,
) -> list[ParserPost]:
    """Fetch text posts for a supplier channel from the parser service."""
    all_posts: list[ParserPost] = []
    async for page in iter_posts(
        channel_id=channel_id,
        from_=from_,
        content_type=content_type,
        http_client=http_client,
    ):
        all_posts.extend(page)
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
        except ValidationError as exc:
            logger.warning("Skipping invalid parser channel: {}", exc)
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


class ParserBackfill(BaseModel):
    task_id: int
    status: str
    channel_id: int
    error_text: str | None = None


async def enqueue_channel_backfill(
    channel_id: int,
    *,
    http_client: httpx.AsyncClient | None = None,
) -> ParserBackfill:
    """Ask tg-runtime to pull recent channel history. Does not run MTProto here."""
    payload = await _request(
        "POST",
        "/channels/backfill",
        json_body={"channel_id": channel_id},
        http_client=http_client,
    )
    if not isinstance(payload, dict):
        raise ParserClientError("parser_invalid_backfill_response")
    try:
        return ParserBackfill.model_validate(payload)
    except ValidationError as exc:
        raise ParserClientError(f"parser_invalid_backfill_response:{exc}") from exc


async def fetch_channel_backfill(
    task_id: int,
    *,
    http_client: httpx.AsyncClient | None = None,
) -> ParserBackfill:
    payload = await _request(
        "GET",
        f"/channels/backfill/{task_id}",
        http_client=http_client,
    )
    if not isinstance(payload, dict):
        raise ParserClientError("parser_invalid_backfill_response")
    try:
        return ParserBackfill.model_validate(payload)
    except ValidationError as exc:
        raise ParserClientError(f"parser_invalid_backfill_response:{exc}") from exc


async def sync_channel_history(
    channel_id: int,
    *,
    timeout: float = 90.0,
    poll_seconds: float = 2.0,
    http_client: httpx.AsyncClient | None = None,
) -> str:
    """Enqueue backfill and wait until the runtime finishes or the wait expires."""
    started = asyncio.get_running_loop().time()
    task = await enqueue_channel_backfill(channel_id, http_client=http_client)
    while task.status in {"new", "processing"}:
        if asyncio.get_running_loop().time() - started >= timeout:
            return task.status
        await asyncio.sleep(poll_seconds)
        task = await fetch_channel_backfill(task.task_id, http_client=http_client)
    return task.status


async def fetch_parser_ready(
    *,
    http_client: httpx.AsyncClient | None = None,
) -> tuple[bool, dict[str, Any]]:
    """Short-timeout readiness probe for morning-price dependency status."""
    try:
        payload = await _request("GET", "/ready", http_client=http_client)
    except ParserClientError:
        return False, {"status": "unreachable"}
    if isinstance(payload, dict) and payload.get("status") == "ok":
        return True, payload
    return False, payload if isinstance(payload, dict) else {"status": "unknown"}
