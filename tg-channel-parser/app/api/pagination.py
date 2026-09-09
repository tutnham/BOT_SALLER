"""Pagination helpers for GET /posts."""

from __future__ import annotations

import base64
from collections.abc import Callable
from datetime import datetime
from typing import TypeVar

from fastapi import HTTPException, status

T = TypeVar("T")


def encode_cursor(post_date: datetime, post_id: int) -> str:
    raw = f"{post_date.isoformat()}|{post_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def decode_cursor(cursor: str) -> tuple[datetime, int]:
    try:
        decoded = base64.urlsafe_b64decode(cursor.encode()).decode()
        date_part, id_part = decoded.rsplit("|", 1)
        return datetime.fromisoformat(date_part), int(id_part)
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid_cursor",
        ) from exc


def paginate_rows(
    rows: list[T],
    *,
    limit: int,
    cursor: str | None,
    get_post_date: Callable[[T], datetime],
    get_row_id: Callable[[T], int],
) -> tuple[list[T], str | None]:
    """Stable pagination by (post_date, id)."""
    ordered = sorted(rows, key=lambda row: (get_post_date(row), get_row_id(row)))
    if cursor is not None:
        cursor_date, cursor_id = decode_cursor(cursor)
        ordered = [
            row
            for row in ordered
            if (get_post_date(row), get_row_id(row)) > (cursor_date, cursor_id)
        ]
    has_more = len(ordered) > limit
    page_rows = ordered[:limit]
    next_cursor = (
        encode_cursor(get_post_date(page_rows[-1]), get_row_id(page_rows[-1]))
        if has_more
        else None
    )
    return page_rows, next_cursor
