"""Unit tests for regex supplier reply parser (TECH DOC §8.3)."""

from __future__ import annotations

import pytest
from app.parsers.regex_parser import parse_supplier_reply


@pytest.mark.parametrize(
    ("text", "expected_price"),
    [
        ("Есть, 85000 руб", 85000.0),
        ("85 000 ₽", 85000.0),
        ("85к", 85000.0),
        ("85 тыс", 85000.0),
        ("85000", 85000.0),
    ],
)
def test_parse_price_patterns(text: str, expected_price: float) -> None:
    parsed = parse_supplier_reply(text)
    assert parsed.price == expected_price
    assert parsed.confidence >= 0.75


def test_parse_availability_positive() -> None:
    parsed = parse_supplier_reply("В наличии, 70000 руб")
    assert parsed.available is True
    assert parsed.price == 70000.0


def test_parse_availability_negative() -> None:
    parsed = parse_supplier_reply("Нет в наличии")
    assert parsed.available is False
    assert parsed.price is None
    assert parsed.confidence == 0.85


def test_parse_availability_negative_before_positive() -> None:
    parsed = parse_supplier_reply("Нет в наличии, но можем найти")
    assert parsed.available is False


def test_parse_qty() -> None:
    parsed = parse_supplier_reply("Есть 85000 руб, кол-во 3")
    assert parsed.qty == 3
    assert parsed.price == 85000.0


def test_parse_qty_sht() -> None:
    parsed = parse_supplier_reply("85000 руб, 5 шт")
    assert parsed.qty == 5


def test_parse_condition() -> None:
    parsed = parse_supplier_reply("Новый, 90000 руб")
    assert parsed.condition == "новый"


def test_parse_low_confidence_no_signals() -> None:
    parsed = parse_supplier_reply("Привет, перезвоните")
    assert parsed.confidence == 0.0
    assert parsed.price is None


def test_parse_empty_text() -> None:
    parsed = parse_supplier_reply("")
    assert parsed.confidence == 0.0


def test_parse_weak_price_below_threshold() -> None:
    """Bare 3-digit number without markers — no price extracted."""
    parsed = parse_supplier_reply("Цена 500")
    assert parsed.price is None
    assert parsed.confidence == 0.0


def test_parse_pod_zakaz_unavailable() -> None:
    parsed = parse_supplier_reply("Под заказ, 85000 руб")
    assert parsed.available is False
    assert parsed.price == 85000.0
