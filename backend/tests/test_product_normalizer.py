"""Shared product normalizer tests."""

from __future__ import annotations

from app.parsers.product_normalizer import extract_product_attrs, normalize_product_text


def test_normalize_cyrillic_and_glued_tokens() -> None:
    assert "iphone 17 pro max" in normalize_product_text("айфон 17промакс")
    assert "iphone 16 pro" in normalize_product_text("айфон 16 про")


def test_extract_attrs_16e() -> None:
    attrs = extract_product_attrs("iPhone 16e 128 black")
    assert attrs.number == "16"
    assert attrs.variant == "e"
