"""Versioned canonical SKU identity for batch requests and daily price projection."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.parsers.product_normalizer import normalize_product_text

NORMALIZER_VERSION = "v1"

_WILDCARD = "*"

_BATCH_IPHONE_COLORS: dict[str, str] = {
    "yellow": "yellow",
    "midnight": "midnight",
    "starlight": "starlight",
    "purple": "purple",
    "red": "red",
    "pink": "pink",
    "blue": "blue",
    "natural": "natural",
    "black": "black",
    "white": "white",
    "teal": "teal",
    "ultramarine": "ultramarine",
    "desert": "desert",
    "gold": "gold",
    "green": "green",
    "silver": "silver",
    "gray": "gray",
    "grey": "gray",
}

_STORAGE_RE = re.compile(
    r"(\d+)\s*(?:гб|gb|гиг(?:абайт)?|tb|тб)\b",
    re.IGNORECASE,
)
_VARIANT_TOKENS = (
    ("pro max", "pro-max"),
    ("promax", "pro-max"),
    ("pro", "pro"),
    ("max", "max"),
    ("plus", "plus"),
    ("air", "air"),
    ("ultra", "ultra"),
    ("mini", "mini"),
    ("se", "se"),
)
_MODEL_16E_RE = re.compile(r"\b16\s*e\b", re.IGNORECASE)
_LEADING_MODEL_RE = re.compile(r"^(\d{1,2})\b")


@dataclass(frozen=True)
class CanonicalSkuAttrs:
    brand: str | None
    family: str | None
    model_segment: str | None
    variant: str | None
    storage_gb: str | None
    color: str | None
    sim: str | None
    region: str | None
    quantity: int
    confidence: float
    ambiguities: tuple[str, ...]
    display_model: str | None


def storage_to_key_gb(raw: str | None) -> str | None:
    if not raw:
        return None
    text = raw.strip().upper()
    if text.endswith("TB"):
        num = text[:-2].strip()
        if num.isdigit():
            return str(int(num) * 1024)
    if text.endswith("GB"):
        num = text[:-2].strip()
        if num.isdigit():
            return num
    match = _STORAGE_RE.search(raw)
    if match:
        unit = match.group(0).lower()
        num = match.group(1)
        if "tb" in unit or "тб" in unit:
            return str(int(num) * 1024)
        return num
    return None


def _slug_token(value: str | None) -> str:
    if not value:
        return _WILDCARD
    return value.strip().lower().replace(" ", "-")


def build_canonical_sku_key(attrs: CanonicalSkuAttrs) -> str | None:
    if not attrs.model_segment:
        return None
    brand = _slug_token(attrs.brand or "apple")
    family = _slug_token(attrs.family or "iphone")
    model = _slug_token(attrs.model_segment)
    variant = _slug_token(attrs.variant) if attrs.variant else _WILDCARD
    storage = _slug_token(attrs.storage_gb) if attrs.storage_gb else _WILDCARD
    color = _slug_token(attrs.color) if attrs.color else _WILDCARD
    sim = _slug_token(attrs.sim) if attrs.sim else _WILDCARD
    region = _slug_token(attrs.region) if attrs.region else _WILDCARD
    return "|".join(
        [
            NORMALIZER_VERSION,
            brand,
            family,
            f"{model}-{variant}" if variant != _WILDCARD else model,
            storage,
            color,
            sim,
            region,
        ]
    )


def _extract_batch_color(text: str) -> str | None:
    lowered = normalize_product_text(text)
    tokens = lowered.split()
    for token in reversed(tokens):
        if token in _BATCH_IPHONE_COLORS:
            return _BATCH_IPHONE_COLORS[token]
    for alias, canonical in _BATCH_IPHONE_COLORS.items():
        if f" {alias} " in f" {lowered} ":
            return canonical
    return None


def _parse_iphone_batch_line(source_text: str) -> CanonicalSkuAttrs:
    raw = (source_text or "").strip()
    text = normalize_product_text(raw)
    ambiguities: list[str] = []
    from app.parsers.product_normalizer import _extract_qty

    qty = _extract_qty(raw) or 1
    work = text

    brand = "Apple"
    family = "iphone"
    model_segment: str | None = None
    variant: str | None = None

    if _MODEL_16E_RE.search(work):
        model_segment = "16"
        variant = "e"
        work = _MODEL_16E_RE.sub("", work).strip()
    else:
        lead = _LEADING_MODEL_RE.match(work)
        if lead:
            model_segment = lead.group(1)
            work = work[lead.end() :].strip()
        for token, slug in _VARIANT_TOKENS:
            if re.search(rf"\b{re.escape(token)}\b", work):
                variant = slug
                work = re.sub(rf"\b{re.escape(token)}\b", " ", work).strip()
                break

    storage_raw = None
    storage_match = _STORAGE_RE.search(raw) or _STORAGE_RE.search(work)
    if storage_match:
        unit = storage_match.group(0).lower()
        storage_raw = f"{storage_match.group(1)}{'TB' if 'tb' in unit or 'тб' in unit else 'GB'}"
        work = work.replace(storage_match.group(0), " ").strip()

    storage_gb = storage_to_key_gb(storage_raw)
    color = _extract_batch_color(raw)

    confidence = 0.5
    if model_segment and storage_gb:
        confidence = 0.92
    elif model_segment:
        confidence = 0.7
    if not model_segment:
        ambiguities.append("model_unknown")
        confidence = min(confidence, 0.4)
    if not storage_gb:
        ambiguities.append("storage_unknown")
        confidence = min(confidence, 0.5)

    display_parts: list[str] = []
    if model_segment:
        if variant == "e":
            display_parts.append(f"{model_segment}e")
        else:
            display_parts.append(model_segment)
            if variant and variant != "base":
                display_parts.append(variant.replace("-", " ").title())
    if storage_raw:
        display_parts.append(storage_raw)
    if color:
        display_parts.append(color.title())

    return CanonicalSkuAttrs(
        brand=brand,
        family=family,
        model_segment=model_segment,
        variant=variant,
        storage_gb=storage_gb,
        color=color,
        sim=None,
        region=None,
        quantity=qty,
        confidence=confidence,
        ambiguities=tuple(ambiguities),
        display_model=" ".join(display_parts) if display_parts else raw,
    )


def parse_batch_line(source_text: str) -> CanonicalSkuAttrs:
    """Deterministic batch-line parser for Apple iPhone shorthand lines."""
    return _parse_iphone_batch_line(source_text)


def attrs_to_normalized_json(attrs: CanonicalSkuAttrs) -> dict[str, Any]:
    storage_display = None
    if attrs.storage_gb:
        gb = int(attrs.storage_gb)
        storage_display = f"{gb // 1024}TB" if gb >= 1024 and gb % 1024 == 0 else f"{gb}GB"
    return {
        "model": attrs.display_model or "",
        "storage": storage_display,
        "color": attrs.color.title() if attrs.color else None,
        "region": attrs.region,
        "sim": attrs.sim,
        "qty": attrs.quantity,
        "condition": None,
        "confidence": attrs.confidence,
        "brand": attrs.brand,
        "family": attrs.family,
        "variant": attrs.variant,
        "ambiguities": list(attrs.ambiguities),
    }


def sku_keys_compatible(request_key: str, candidate_key: str) -> bool:
    """Exact match only; wildcards in keys do not broaden reuse."""
    return request_key == candidate_key


def daily_price_compatible_with_request(
    request_key: str,
    projection_key: str,
    *,
    request_color: str | None,
) -> bool:
    if request_key == projection_key:
        return True
    if request_color:
        return False
    return False
