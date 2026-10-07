"""Regex parser for supplier replies (TECH DOC §8.3, Phase 2 — no LLM)."""

from __future__ import annotations

import re
from decimal import Decimal

from pydantic import BaseModel, Field

from app.parsers.price_parse import parse_price_from_text

# --- Availability patterns (negative checked first) --------------------------------

_NEGATIVE_AVAILABILITY = re.compile(
    r"(?:"
    r"нет\s+в\s+наличии|"
    r"нет\s+в\s+налич|"
    r"не\s+в\s+наличии|"
    r"закончил(?:ся|ась|ось|и)?|"
    r"распродан(?:о|ы)?|"
    r"под\s+заказ|"
    r"\bнет\b"
    r")",
    re.IGNORECASE,
)

_POSITIVE_AVAILABILITY = re.compile(
    r"(?:"
    r"в\s+наличии|"
    r"в\s+налич|"
    r"имеется|"
    r"\bесть\b"
    r")",
    re.IGNORECASE,
)

# --- Quantity ----------------------------------------------------------------------

_QTY_PATTERN = re.compile(
    r"(?:"
    r"(?:кол[-\s]?во|количество)\s*[:\-]?\s*(\d+)|"
    r"(\d+)\s*(?:шт\.?|штук(?:а|и)?)"
    r")",
    re.IGNORECASE,
)

# --- Condition ---------------------------------------------------------------------

_CONDITION_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\btrade[-\s]?in\b", re.IGNORECASE), "trade-in"),
    (re.compile(r"восстановлен\w*", re.IGNORECASE), "восстановленный"),
    (re.compile(r"\bб/?\s*у\b", re.IGNORECASE), "б/у"),
    (re.compile(r"\bнов(?:ый|ая|ое|ые)\b", re.IGNORECASE), "новый"),
]

class ParsedSupplierReply(BaseModel):
    """Structured supplier reply aligned with TECH DOC §8.3."""

    available: bool | None = None
    qty: int | None = None
    price: Decimal | None = None
    min_sale_price: Decimal | None = None
    condition: str | None = None
    note: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)


def _normalize_number(raw: str) -> Decimal | None:
    """Parse numeric string with optional spaces as thousands separator."""
    cleaned = raw.replace(" ", "").replace("\u00a0", "").strip()
    if not cleaned:
        return None
    try:
        return Decimal(cleaned)
    except ValueError:
        return None


def _extract_availability(text: str) -> bool | None:
    if _NEGATIVE_AVAILABILITY.search(text):
        return False
    if _POSITIVE_AVAILABILITY.search(text):
        return True
    return None


def _extract_qty(text: str) -> int | None:
    match = _QTY_PATTERN.search(text)
    if not match:
        return None
    raw = match.group(1) or match.group(2)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def extract_condition(text: str) -> str | None:
    """Extract device condition from free text."""
    for pattern, label in _CONDITION_PATTERNS:
        if pattern.search(text):
            return label
    return None


def _extract_price(text: str) -> tuple[Decimal | None, str]:
    """
    Extract price and signal strength.

    Returns:
        (price, strength) where strength is 'strong' | 'weak' | 'none'
    """
    result = parse_price_from_text(text)
    if result.price is None:
        return None, "none"
    strength = "strong" if result.confidence >= 0.75 else "weak"
    return result.price, strength


def _compute_confidence(
    *,
    price: Decimal | None,
    price_strength: str,
    available: bool | None,
    qty: int | None,
) -> float:
    if price is not None:
        base = 0.9 if price_strength == "strong" else 0.6
        if qty is not None:
            base = min(base + 0.05, 0.95)
        return base

    if available is not None:
        return 0.85

    return 0.0


def parse_supplier_reply(raw_text: str) -> ParsedSupplierReply:
    """
    Parse supplier free-text reply into structured fields (regex only).

    No LLM calls. ``min_sale_price`` and ``note`` are not extracted in Phase 2.
    """
    text = (raw_text or "").strip()
    if not text:
        return ParsedSupplierReply(confidence=0.0)

    available = _extract_availability(text)
    qty = _extract_qty(text)
    condition = extract_condition(text)
    price, price_strength = _extract_price(text)

    confidence = _compute_confidence(
        price=price,
        price_strength=price_strength,
        available=available,
        qty=qty,
    )

    return ParsedSupplierReply(
        available=available,
        qty=qty,
        price=price,
        min_sale_price=None,
        condition=condition,
        note=None,
        confidence=confidence,
    )
