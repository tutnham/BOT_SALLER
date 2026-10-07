"""Supplier price extraction (Decimal-only, auditable)."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import BaseModel, Field

from app.config import get_settings

PriceParseMethod = Literal[
    "full_integer",
    "decimal_thousands",
    "implicit_thousands",
    "currency_marked",
    "spaced_thousands",
    "llm",
    "none",
]

# Whole token: optional int part + optional . or , decimal + k/к/тыс
_PRICE_SUFFIX_DECIMAL = re.compile(
    r"(?<!\d)"
    r"(\d[\d\s]*(?:[.,]\d+)?)\s*"
    r"(?:к|k|тыс\.?|т\.?)"
    r"(?:\s*(?:руб\.?|₽|р\.?))?"
    r"(?!\w)",
    re.IGNORECASE,
)

_PRICE_WITH_CURRENCY = re.compile(
    r"(\d[\d\s]+)\s*(?:руб\.?|₽|р\.?)(?:\s|$|[,.])",
    re.IGNORECASE,
)

_PRICE_SPACED = re.compile(r"\b(\d(?:[\d\s]{2,}))\b")

_PRICE_BARE = re.compile(r"\b(\d{4,})\b")

# Product-line context: storage, model tokens to strip before implicit price
_STORAGE_TOKEN = re.compile(
    r"\b\d+\s*(?:гб|gb|гиг(?:абайт)?|tb|тб)\b",
    re.IGNORECASE,
)
_SKU_TOKEN = re.compile(r"\bSM-[A-Z0-9]+\b", re.IGNORECASE)
_SIM_QTY_TOKEN = re.compile(r"\b\d\s*sim\b", re.IGNORECASE)

# Implicit thousands at end of line (after product context)
_IMPLICIT_END = re.compile(
    r"(?<!\d)(\d{1,3}(?:[.,]\d{1,2})?)\s*$",
)

# Variant tokens that may precede trailing price
_VARIANT_TOKENS = re.compile(
    r"\b(?:pro\s*max|promax|pro|max|plus|ultra|mini|se|air|esim|nano)\b",
    re.IGNORECASE,
)


class PriceParseResult(BaseModel):
    price: Decimal | None = None
    available: bool | None = None
    qty: int | None = None
    source_fragment: str | None = None
    method: PriceParseMethod = "none"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    validation_errors: list[str] = Field(default_factory=list)
    original_token: str | None = None

    def as_audit_dict(self) -> dict[str, object]:
        return {
            "original_token": self.original_token,
            "normalized_price": str(self.price) if self.price is not None else None,
            "parse_method": self.method,
            "confidence": self.confidence,
            "validation_errors": self.validation_errors,
        }


def _parse_decimal_token(raw: str) -> Decimal | None:
    cleaned = raw.replace(" ", "").replace("\u00a0", "").strip()
    if not cleaned:
        return None
    if "," in cleaned and "." in cleaned:
        return None
    if "," in cleaned:
        cleaned = cleaned.replace(",", ".")
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def _apply_sanity(value: Decimal, errors: list[str]) -> Decimal | None:
    settings = get_settings()
    if value < settings.supplier_price_min_rub:
        errors.append("below_min")
        return None
    if value > settings.supplier_price_max_rub:
        errors.append("above_max")
        return None
    return value


def _has_product_line_context(text: str) -> bool:
    """Line looks like a product offer, not a bare number."""
    if _STORAGE_TOKEN.search(text):
        return True
    if _SKU_TOKEN.search(text):
        return True
    if _VARIANT_TOKENS.search(text):
        return True
    if _SIM_QTY_TOKEN.search(text):
        return True
    # Two-digit model at start (e.g. 17 pro ...)
    if re.search(r"(?<!\d)\b\d{1,2}\b", text) and len(text.split()) >= 2:
        return True
    return False


def _collect_explicit_price_candidates(
    text: str,
) -> list[tuple[Decimal, str, PriceParseMethod, str]]:
    found: list[tuple[Decimal, str, PriceParseMethod, str]] = []

    for match in _PRICE_SUFFIX_DECIMAL.finditer(text):
        token = match.group(1)
        base = _parse_decimal_token(token)
        if base is None:
            continue
        value = base * Decimal(1000)
        found.append((value, match.group(0).strip(), "decimal_thousands", token))

    for match in _PRICE_WITH_CURRENCY.finditer(text):
        token = match.group(1)
        value = _parse_decimal_token(token)
        if value is None:
            continue
        found.append((value, match.group(0).strip(), "currency_marked", token))

    for match in _PRICE_SPACED.finditer(text):
        token = match.group(1)
        value = _parse_decimal_token(token)
        if value is not None and value >= 1000:
            found.append((value, match.group(0).strip(), "spaced_thousands", token))

    for match in _PRICE_BARE.finditer(text):
        token = match.group(1)
        if _STORAGE_TOKEN.search(token):
            continue
        value = _parse_decimal_token(token)
        if value is not None:
            found.append((value, match.group(0).strip(), "full_integer", token))

    return found


def _collect_implicit_candidate(
    text: str,
) -> list[tuple[Decimal, str, PriceParseMethod, str]]:
    settings = get_settings()
    if not settings.supplier_implicit_thousands_enabled:
        return []
    if not _has_product_line_context(text):
        return []
    if re.search(r"(?:кол[-\s]?во|количество|шт\.?|штук)\s*\d+\s*$", text, re.IGNORECASE):
        return []
    stripped = _STORAGE_TOKEN.sub("", text)
    stripped = _SKU_TOKEN.sub("", stripped)
    end_match = _IMPLICIT_END.search(stripped.strip())
    if not end_match:
        return []
    token = end_match.group(1)
    base = _parse_decimal_token(token)
    if base is None:
        return []
    if "." in token or "," in token:
        value = base * Decimal(1000)
        method: PriceParseMethod = "implicit_thousands"
    elif base < 1000:
        value = base * Decimal(1000)
        method = "implicit_thousands"
    else:
        return []
    return [(value, end_match.group(0).strip(), method, token)]


def parse_price_from_text(raw_text: str) -> PriceParseResult:
    text = (raw_text or "").strip()
    errors: list[str] = []
    if not text:
        return PriceParseResult(validation_errors=["empty"])

    candidates = _collect_explicit_price_candidates(text)
    if not candidates:
        candidates = _collect_implicit_candidate(text)
    if not candidates:
        return PriceParseResult(method="none", confidence=0.0)

    # Deduplicate by normalized value; if multiple distinct values, reject
    by_value: dict[str, tuple[Decimal, str, PriceParseMethod, str]] = {}
    for value, fragment, method, token in candidates:
        key = str(value)
        if key not in by_value:
            by_value[key] = (value, fragment, method, token)

    if len(by_value) > 1:
        if _has_product_line_context(text):
            sane: list[tuple[Decimal, str, PriceParseMethod, str]] = []
            for value, fragment, method, token in candidates:
                probe_errors: list[str] = []
                if _apply_sanity(value, probe_errors) is not None:
                    sane.append((value, fragment, method, token))
            pool = sane or candidates
            ranked = sorted(
                pool,
                key=lambda item: (
                    text.rfind(item[3]),
                    {"decimal_thousands": 4, "currency_marked": 3, "full_integer": 2, "spaced_thousands": 1}.get(
                        item[2], 0
                    ),
                ),
                reverse=True,
            )
            value, fragment, method, token = ranked[0]
        else:
            return PriceParseResult(
                method="none",
                confidence=0.0,
                validation_errors=["ambiguous_multiple_prices"],
            )
    else:
        value, fragment, method, token = next(iter(by_value.values()))
    validated = _apply_sanity(value, errors)
    if validated is None:
        return PriceParseResult(
            method=method,
            original_token=token,
            source_fragment=fragment,
            confidence=0.0,
            validation_errors=errors,
        )

    conf = 0.9 if method in ("decimal_thousands", "full_integer", "currency_marked") else 0.85
    if method == "implicit_thousands":
        conf = 0.8

    return PriceParseResult(
        price=validated,
        source_fragment=fragment,
        method=method,
        confidence=conf,
        original_token=token,
        validation_errors=errors,
    )


def is_bare_full_price_only(text: str, price: Decimal | None) -> bool:
    """True when message is essentially only a full integer price (e.g. 127500)."""
    if price is None:
        return False
    stripped = re.sub(r"\s+", "", text.strip())
    bare = str(int(price))
    return stripped == bare or stripped == f"{bare}₽" or stripped == f"{bare}руб"
