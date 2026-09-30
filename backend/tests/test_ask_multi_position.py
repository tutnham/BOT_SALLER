"""Multi-position /ask splits into separate product segments."""

from __future__ import annotations

from app.services.product_classifier import split_positions


def test_split_positions_two_lines() -> None:
    segments = split_positions("iPhone 17 256GB\nSamsung Galaxy S25 256")
    assert len(segments) == 2
    assert "iPhone" in segments[0]
    assert "Samsung" in segments[1]


def test_split_positions_semicolon() -> None:
    segments = split_positions("MacBook Air M3; iPad 11")
    assert len(segments) == 2


def test_single_product_not_split() -> None:
    text = "iPhone 17 Pro Max 256"
    assert split_positions(text) == [text]
