from __future__ import annotations

from app.llm.client import validate_supplier_reply_payload


def test_validate_supplier_reply_payload_accepts_valid_schema() -> None:
    parsed = validate_supplier_reply_payload(
        {
            "available": True,
            "qty": 2,
            "price": 85000,
            "min_sale_price": None,
            "condition": "новый",
            "note": None,
            "confidence": 0.83,
        }
    )
    assert parsed.price == 85000
    assert parsed.qty == 2
    assert parsed.confidence == 0.83


def test_validate_supplier_reply_payload_invalid_json_maps_to_confidence_zero() -> None:
    parsed = validate_supplier_reply_payload("not-a-json-object")
    assert parsed.confidence == 0.0
    assert parsed.price is None
    assert parsed.qty is None
