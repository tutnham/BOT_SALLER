"""Unit tests for posts pagination helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.pagination import decode_cursor, encode_cursor, paginate_rows


def _row(row_id: int, minute: int) -> SimpleNamespace:
    return SimpleNamespace(
        id=row_id,
        post_date=datetime(2026, 7, 30, 6, minute, tzinfo=UTC),
    )


def test_encode_decode_cursor_roundtrip() -> None:
    post_date = datetime(2026, 7, 30, 6, 0, tzinfo=UTC)
    cursor = encode_cursor(post_date, 42)
    decoded_date, decoded_id = decode_cursor(cursor)
    assert decoded_id == 42
    assert decoded_date == post_date


def test_decode_cursor_invalid_raises() -> None:
    with pytest.raises(HTTPException) as exc:
        decode_cursor("not-a-valid-cursor")
    assert exc.value.status_code == 400


def test_paginate_rows_returns_next_cursor() -> None:
    rows = [_row(1, 0), _row(2, 1), _row(3, 2)]
    page, next_cursor = paginate_rows(
        rows,
        limit=2,
        cursor=None,
        get_post_date=lambda row: row.post_date,
        get_row_id=lambda row: row.id,
    )
    assert [row.id for row in page] == [1, 2]
    assert next_cursor is not None

    page2, next_cursor2 = paginate_rows(
        rows,
        limit=2,
        cursor=next_cursor,
        get_post_date=lambda row: row.post_date,
        get_row_id=lambda row: row.id,
    )
    assert [row.id for row in page2] == [3]
    assert next_cursor2 is None
