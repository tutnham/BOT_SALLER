"""Deterministic regex/dictionary extraction for employee request text (TECH DOC §9.1)."""

from __future__ import annotations

import re
from typing import Any

from app.parsers.regex_parser import extract_condition

_BRAND_ALIASES: dict[str, str] = {
    "iphone": "iPhone",
    "айфон": "iPhone",
    "ipad": "iPad",
    "macbook": "MacBook",
    "airpods": "AirPods",
    "samsung": "Samsung",
    "galaxy": "Samsung",
    "xiaomi": "Xiaomi",
    "redmi": "Redmi",
    "pixel": "Pixel",
}

_COLOR_ALIASES: dict[str, str] = {
    "чёрный": "чёрный",
    "черный": "чёрный",
    "black": "Black",
    "белый": "белый",
    "white": "White",
    "синий": "синий",
    "blue": "Blue",
    "золотой": "золотой",
    "gold": "Gold",
    "фиолетовый": "фиолетовый",
    "purple": "Purple",
    "зелёный": "зелёный",
    "зеленый": "зелёный",
    "green": "Green",
    "серый": "серый",
    "gray": "Gray",
    "grey": "Gray",
    "титан": "титан",
    "titanium": "Titanium",
}

_STORAGE_RE = re.compile(
    r"(\d+)\s*(?:гб|gb|гиг(?:абайт)?|tb|тб)\b",
    re.IGNORECASE,
)
_QTY_RE = re.compile(
    r"(?:"
    r"(\d+)\s*(?:шт\.?|штук(?:а|и)?)|"
    r"(?:кол[-\s]?во|количество)\s*[:\-]?\s*(\d+)"
    r")",
    re.IGNORECASE,
)
_REGION_RE = re.compile(
    r"\b(?:LL/A|RU/A|ZP/A|CH/A|JP/A|EU/A|CH|JP|EU|US/A|US)\b",
    re.IGNORECASE,
)
_ROSTEST_RE = re.compile(r"\b(?:ростест|рст)\b", re.IGNORECASE)
_SIM_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\besim\b", re.IGNORECASE), "eSIM"),
    (re.compile(r"dual\s*sim|2\s*sim|две\s*sim", re.IGNORECASE), "Dual SIM"),
    (re.compile(r"nano[-\s]?sim", re.IGNORECASE), "nano-SIM"),
]
_MODEL_SUFFIX_RE = re.compile(
    r"\b(?:pro\s*max|pro|max|plus|ultra|mini|se)\b",
    re.IGNORECASE,
)
_MODEL_NUMBER_RE = re.compile(r"\b(\d{1,2})\b")


def _normalize_storage(raw: str, unit: str) -> str:
    unit_lower = unit.lower()
    if unit_lower in {"tb", "тб"}:
        return f"{raw}TB"
    return f"{raw}GB"


def _extract_storage(text: str) -> str | None:
    match = _STORAGE_RE.search(text)
    if not match:
        return None
    raw = match.group(1)
    unit_match = re.search(r"(гб|gb|tb|тб)", match.group(0), re.IGNORECASE)
    unit = unit_match.group(1) if unit_match else "gb"
    return _normalize_storage(raw, unit)


def _extract_color(text: str) -> str | None:
    lowered = text.lower()
    for alias, canonical in _COLOR_ALIASES.items():
        if alias in lowered:
            return canonical
    return None


def _extract_region(text: str) -> str | None:
    if _ROSTEST_RE.search(text):
        return "RU"
    match = _REGION_RE.search(text)
    if match:
        return match.group(0).upper().replace("/A", "")
    return None


def _extract_sim(text: str) -> str | None:
    for pattern, label in _SIM_PATTERNS:
        if pattern.search(text):
            return label
    return None


def _extract_qty(text: str) -> int | None:
    match = _QTY_RE.search(text)
    if not match:
        return None
    raw = match.group(1) or match.group(2)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _extract_model(text: str) -> str | None:
    lowered = text.lower()
    brand: str | None = None
    brand_start = len(text)

    for alias, canonical in _BRAND_ALIASES.items():
        idx = lowered.find(alias)
        if idx == -1:
            continue
        if idx < brand_start:
            brand_start = idx
            brand = canonical

    if brand is None:
        return None

    tail = text[brand_start:]
    number_match = _MODEL_NUMBER_RE.search(tail)
    suffix_match = _MODEL_SUFFIX_RE.search(tail)

    parts: list[str] = [brand]
    if number_match:
        parts.append(number_match.group(1))
    if suffix_match:
        suffix = suffix_match.group(0).strip()
        if suffix.lower() == "pro max":
            parts.extend(["Pro", "Max"])
        else:
            parts.append(suffix.title())

    model = " ".join(parts).strip()
    return model or None


def _compute_confidence(
    *,
    model: str | None,
    storage: str | None,
    color: str | None,
    qty: int | None,
) -> float:
    if model and storage:
        return 0.9
    if model and (color is not None or qty is not None):
        return 0.8
    if model:
        return 0.6
    return 0.0


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

    model = _extract_model(text)
    storage = _extract_storage(text)
    color = _extract_color(text)
    region = _extract_region(text)
    sim = _extract_sim(text)
    qty = _extract_qty(text)
    condition = extract_condition(text)
    confidence = _compute_confidence(
        model=model,
        storage=storage,
        color=color,
        qty=qty,
    )

    return {
        "model": model or "",
        "storage": storage,
        "color": color,
        "region": region,
        "sim": sim,
        "qty": qty,
        "condition": condition,
        "confidence": confidence,
    }
