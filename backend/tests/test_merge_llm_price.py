"""merge_llm_price helper."""

from __future__ import annotations

from decimal import Decimal

from app.parsers.regex_parser import ParsedSupplierReply
from app.services.reply_binding_service import merge_llm_price


def test_merge_llm_price_fills_missing_regex_price() -> None:
    parsed = ParsedSupplierReply(price=None, confidence=0.5)
    merged = merge_llm_price(parsed, Decimal("77000"))
    assert merged.price == Decimal("77000")
    assert merged.confidence >= 0.85


def test_merge_llm_price_keeps_existing_price() -> None:
    parsed = ParsedSupplierReply(price=Decimal("80000"), confidence=0.9)
    merged = merge_llm_price(parsed, Decimal("77000"))
    assert merged.price == Decimal("80000")
