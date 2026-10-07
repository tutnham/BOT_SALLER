"""Unit tests for supplier price parsing (plan §11.1–6)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.parsers.price_parse import parse_price_from_text
from app.parsers.regex_parser import parse_supplier_reply


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("127.5к", Decimal("127500")),
        ("127,5к", Decimal("127500")),
        ("127.5k", Decimal("127500")),
        ("90к", Decimal("90000")),
        ("127500", Decimal("127500")),
        ("127 500", Decimal("127500")),
    ],
)
def test_decimal_thousands_and_full_integer(text: str, expected: Decimal) -> None:
    result = parse_price_from_text(text)
    assert result.price == expected
    assert result.price != Decimal("5000")


def test_product_line_implicit_98() -> None:
    result = parse_price_from_text("17 pro 256 blue eSIM 98")
    assert result.price == Decimal("98000")


def test_product_line_implicit_93_1() -> None:
    result = parse_price_from_text("17 pro 256 blue eSIM 93.1")
    assert result.price == Decimal("93100")


def test_product_line_implicit_119_5() -> None:
    result = parse_price_from_text("18 Pro Max 256GB Glacier Esim 119.5")
    assert result.price == Decimal("119500")


def test_standalone_98_not_price() -> None:
    result = parse_price_from_text("98")
    assert result.price is None


def test_storage_256gb_not_price() -> None:
    result = parse_price_from_text("256GB")
    assert result.price is None


def test_model_18_not_price() -> None:
    result = parse_price_from_text("18 Pro Max")
    assert result.price is None


def test_full_line_with_suffix_k_via_reply_parser() -> None:
    parsed = parse_supplier_reply("18 Pro Max 256GB Glacier Esim 127.5к")
    assert parsed.price == Decimal("127500")
