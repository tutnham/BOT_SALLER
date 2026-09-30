"""Parametrized markup rules per customer spec."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.services.markup_service import get_markup, seed_markup_rule_rows


@pytest.fixture
def rules():
    return seed_markup_rule_rows()


@pytest.mark.parametrize(
    ("product", "expected"),
    [
        ("iPhone 17 Air", 500),
        ("iPhone 17 E", 500),
        ("iPhone 17 Pro", 800),
        ("iPhone 17 Pro Max", 800),
        ("iPhone 17 256", 500),
        ("iPhone 16 Pro", 500),
        ("iPhone 16 Pro Max", 800),
        ("iPhone 16 Plus", 500),
        ("iPhone 16e", 500),
        ("iPhone 15 Pro Max", 500),
        ("iPhone 14 Pro Max", 500),
        ("iPhone 13", 500),
        ("iPad Pro", 500),
        ("Apple Watch Ultra", 500),
        ("AirPods Pro", 500),
        ("MacBook Pro 14", 1000),
        ("Samsung Galaxy S24", 800),
    ],
)
def test_customer_markup_rules(product: str, expected: int, rules) -> None:
    result = get_markup(product, rules)
    assert result.markup == Decimal(expected)
    assert result.is_fallback is False


def test_unknown_product_fallback(caplog, rules) -> None:
    result = get_markup("Dyson Airwrap", rules, default=Decimal("500"))
    assert result.markup == Decimal("500")
    assert result.is_fallback is True
