"""Deterministic regex/dictionary extraction for employee request text (TECH DOC §9.1)."""

from __future__ import annotations

from typing import Any

from app.parsers.product_normalizer import (
    compute_request_confidence,
    extract_product_attrs,
)


def parse_request_text(source_text: str) -> dict[str, Any]:
    """
    Regex/dictionary pass for employee request normalization.

    Returns dict aligned with NormalizedRequest / normalized_json shape.
    """
    text = (source_text or "").strip()
    if not text:
        return {
            "model": "",
            "storage": None,
            "color": None,
            "region": None,
            "sim": None,
            "qty": None,
            "condition": None,
            "confidence": 0.0,
        }

    attrs = extract_product_attrs(text)
    confidence = compute_request_confidence(attrs)
    return attrs.as_normalized_json(confidence)
