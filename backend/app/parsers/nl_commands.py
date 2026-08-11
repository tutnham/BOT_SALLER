"""Natural-language synonyms for /bargain, /recheck, and /deal (TECH DOC §11)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Literal

_REQUEST_ID_RE = re.compile(r"#(\d+)")
# Digits not part of a #request_id token (avoid treating #12 as a price).
_PRICE_RE = re.compile(
    r"(?<![#\d])(\d[\d\s]*(?:[.,]\d+)?)\s*(?:₽|руб(?:\.|лей)?)?",
    re.IGNORECASE,
)
_SUPPLIER_ID_RE = re.compile(
    r"(?:поставщик(?:а|у|ом)?|supplier)\s*#?(\d+)",
    re.IGNORECASE,
)
_DEAL_PRICE_RE = re.compile(
    r"(?:за|по\s+цене|цена)\s*(\d[\d\s]*(?:[.,]\d+)?)\s*(?:₽|руб(?:\.|лей)?)?",
    re.IGNORECASE,
)

_RECHECK_PATTERNS = (
    re.compile(r"актуализируй\s+цен", re.IGNORECASE),
    re.compile(r"актуальн\w*\s+ли\s+цен", re.IGNORECASE),
    re.compile(r"проверь\s+цен", re.IGNORECASE),
)

_BARGAIN_PATTERNS = (
    re.compile(r"пройд[её]м\s+ли\s+по\s+цене\s+ниже", re.IGNORECASE),
    re.compile(r"можно\s+дешевле", re.IGNORECASE),
    re.compile(r"поторгуйся", re.IGNORECASE),
)

_DEAL_PATTERNS = (
    re.compile(r"\bберу\b", re.IGNORECASE),
    re.compile(r"\bбер[её]м\b", re.IGNORECASE),
    re.compile(r"закрыва(?:ем|й)\s+сделку", re.IGNORECASE),
)

_HOURS_RE = re.compile(r"(?:через\s+)?(\d+)\s*ч(?:ас(?:а|ов)?)?", re.IGNORECASE)


@dataclass(frozen=True)
class NLCommand:
    intent: Literal["bargain", "recheck", "deal"]
    request_id: int | None
    target_price: Decimal | None = None
    hours: int | None = None
    supplier_id: int | None = None
    price: Decimal | None = None


def _extract_request_id(text: str, reply_text: str | None) -> int | None:
    for candidate in (text, reply_text or ""):
        match = _REQUEST_ID_RE.search(candidate)
        if match:
            return int(match.group(1))
    return None


def _parse_price_token(raw: str) -> Decimal | None:
    cleaned = raw.replace(" ", "").replace(",", ".")
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def _extract_price(text: str) -> Decimal | None:
    matches = list(_PRICE_RE.finditer(text))
    if not matches:
        return None
    # Prefer last numeric token (often the target price after the phrase).
    return _parse_price_token(matches[-1].group(1))


def _extract_deal_price(text: str) -> Decimal | None:
    match = _DEAL_PRICE_RE.search(text)
    if match:
        return _parse_price_token(match.group(1))
    matches = list(_PRICE_RE.finditer(text))
    if not matches:
        return None
    return _parse_price_token(matches[-1].group(1))


def _extract_supplier_id(text: str) -> int | None:
    match = _SUPPLIER_ID_RE.search(text)
    if not match:
        return None
    return int(match.group(1))


def _extract_hours(text: str) -> int | None:
    match = _HOURS_RE.search(text)
    if not match:
        return None
    return int(match.group(1))


def parse_nl_command(
    text: str,
    *,
    reply_text: str | None = None,
) -> NLCommand | None:
    """
    Map free-text employee phrases to bargain/recheck/deal intents.

    Returns None when no synonym matches.
    """
    stripped = text.strip()
    if not stripped or stripped.startswith("/"):
        return None

    request_id = _extract_request_id(stripped, reply_text)

    for pattern in _RECHECK_PATTERNS:
        if pattern.search(stripped):
            return NLCommand(
                intent="recheck",
                request_id=request_id,
                hours=_extract_hours(stripped),
            )

    for pattern in _BARGAIN_PATTERNS:
        if pattern.search(stripped):
            return NLCommand(
                intent="bargain",
                request_id=request_id,
                target_price=_extract_price(stripped),
            )

    for pattern in _DEAL_PATTERNS:
        if pattern.search(stripped):
            return NLCommand(
                intent="deal",
                request_id=request_id,
                supplier_id=_extract_supplier_id(stripped),
                price=_extract_deal_price(stripped),
            )

    return None
