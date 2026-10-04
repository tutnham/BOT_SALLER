"""Decision-table regression tests for supplier reply binding."""

from __future__ import annotations

import pytest

from app.services.reply_binding_service import is_neutral_or_noise


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", "empty"),
        ("привет", "neutral"),
        ("OK!", "neutral"),
        ("https://t.me/foo", "advertisement"),
        ("iPhone 17 117900", None),
        ("117900", None),
    ],
)
def test_is_neutral_or_noise_table(text: str, expected: str | None) -> None:
    assert is_neutral_or_noise(text) == expected
