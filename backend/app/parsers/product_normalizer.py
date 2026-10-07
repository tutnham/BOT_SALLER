"""Shared product text normalization and attribute extraction for requests, markup, routing."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.parsers.regex_parser import extract_condition

_BRAND_ALIASES: dict[str, str] = {
    "iphone": "iPhone",
    "айфон": "iPhone",
    "ipad": "iPad",
    "айпад": "iPad",
    "macbook": "MacBook",
    "макбук": "MacBook",
    "airpods": "AirPods",
    "эирподс": "AirPods",
    "apple watch": "Apple Watch",
    "applewatch": "Apple Watch",
    "iwatch": "Apple Watch",
    "samsung": "Samsung",
    "galaxy": "Samsung",
    "самсунг": "Samsung",
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
    "silver": "Silver",
    "серебро": "Silver",
    "серебристый": "Silver",
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
    "glacier": "Glacier",
    "глетчер": "Glacier",
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
    (re.compile(r"\b1\s*sim\b", re.IGNORECASE), "1 SIM"),
]
_SKU_RE = re.compile(r"\b(SM-[A-Z0-9]+)\b", re.IGNORECASE)
_SAMSUNG_MODEL_RE = re.compile(r"\bS(\d{1,2})\b", re.IGNORECASE)
_MODEL_SUFFIX_RE = re.compile(
    r"\b(?:pro\s*max|promax|pro|max|plus|ultra|mini|se|air|e\b)\b",
    re.IGNORECASE,
)
_MODEL_NUMBER_RE = re.compile(r"\b(\d{1,2})\b")
_16E_RE = re.compile(r"\b16\s*e\b", re.IGNORECASE)

# Cyrillic/Latin homoglyphs for normalization pass
_CYR_TO_LAT = str.maketrans(
    {
        "а": "a",
        "е": "e",
        "о": "o",
        "р": "p",
        "с": "c",
        "у": "y",
        "х": "x",
        "м": "m",
        "к": "k",
        "т": "t",
        "в": "b",
        "н": "h",
        "А": "A",
        "Е": "E",
        "О": "O",
        "Р": "P",
        "С": "C",
        "У": "Y",
        "Х": "X",
        "М": "M",
        "К": "K",
        "Т": "T",
        "В": "B",
        "Н": "H",
    }
)


@dataclass(frozen=True)
class ProductAttrs:
    brand: str | None
    family: str | None
    number: str | None
    variant: str | None
    storage: str | None
    color: str | None
    region: str | None
    sim: str | None
    qty: int | None
    condition: str | None
    model: str | None
    sku: str | None = None

    def as_normalized_json(self, confidence: float) -> dict[str, Any]:
        return {
            "model": self.model or "",
            "storage": self.storage,
            "color": self.color,
            "region": self.region,
            "sim": self.sim,
            "qty": self.qty,
            "condition": self.condition,
            "confidence": confidence,
        }


def normalize_product_text(text: str) -> str:
    """Lowercase, unify scripts, expand glued tokens (17pro -> 17 pro)."""
    raw = (text or "").strip().lower()
    if not raw:
        return ""
    raw = raw.replace("ё", "e")
    raw = re.sub(r"\s+", " ", raw)
    raw = raw.replace("promax", "pro max")
    raw = raw.replace("промакс", "pro max")
    raw = raw.replace("про макс", "pro max")
    raw = raw.replace("про", "pro")
    raw = raw.replace("макс", "max")
    raw = raw.replace("эйр", "air")
    raw = raw.replace("айфон", "iphone")
    raw = raw.replace("айпад", "ipad")
    raw = raw.replace("макбук", "macbook")
    raw = raw.replace("самсунг", "samsung")
    raw = re.sub(r"\b(1[3-9])\s*e\b", r"\1 e", raw)
    raw = re.sub(r"(\d)(pro|max|plus|air|ultra|mini|se)\b", r"\1 \2", raw)
    raw = re.sub(r"(\d)(pro|max)\b", r"\1 \2", raw)
    return raw


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


def _detect_brand(text: str) -> tuple[str | None, str]:
    """Return (canonical brand, tail from brand start)."""
    lowered = text.lower()
    brand: str | None = None
    brand_start = len(text)
    best_len = 0

    for alias, canonical in sorted(_BRAND_ALIASES.items(), key=lambda x: -len(x[0])):
        idx = lowered.find(alias)
        if idx == -1:
            continue
        if idx < brand_start or (idx == brand_start and len(alias) > best_len):
            brand_start = idx
            brand = canonical
            best_len = len(alias)

    if brand is None:
        return None, text
    return brand, text[brand_start:]


def _parse_variant(tail: str, brand: str) -> tuple[str | None, str | None, str | None]:
    """
    Parse family/number/variant from tail after brand.

    family: iphone, ipad, macbook, airpods, apple watch, samsung, ...
    number: 13, 16, 17
    variant: pro max, pro, max, plus, air, e, ultra, mini, se, base
    """
    family: str | None = None
    number: str | None = None
    variant: str | None = None

    if brand == "iPhone":
        family = "iphone"
    elif brand == "iPad":
        family = "ipad"
    elif brand == "MacBook":
        family = "macbook"
    elif brand == "AirPods":
        family = "airpods"
    elif brand == "Apple Watch":
        family = "apple_watch"
    elif brand == "Samsung":
        family = "samsung"
    else:
        family = brand.lower() if brand else None

    if _16E_RE.search(tail):
        number = "16"
        variant = "e"
        return family, number, variant

    number_match = _MODEL_NUMBER_RE.search(tail)
    if number_match:
        number = number_match.group(1)

    suffix_match = _MODEL_SUFFIX_RE.search(tail)
    if suffix_match:
        suffix = suffix_match.group(0).strip().lower()
        if suffix in {"pro max", "promax"}:
            variant = "pro max"
        elif suffix == "pro":
            variant = "pro"
        elif suffix == "max":
            variant = "max"
        elif suffix == "plus":
            variant = "plus"
        elif suffix == "air":
            variant = "air"
        elif suffix == "e":
            variant = "e"
        elif suffix == "ultra":
            variant = "ultra"
        elif suffix == "mini":
            variant = "mini"
        elif suffix == "se":
            variant = "se"
    elif number and family == "iphone":
        variant = "base"

    return family, number, variant


def _parse_brandless_apple_like(text: str) -> tuple[str | None, str | None, str | None, str | None]:
    """
    Parse number/variant without brand (e.g. 18 Pro Max).

    Does not assign family globally; family stays None unless Samsung SKU/S-series.
    """
    family: str | None = None
    brand: str | None = None
    sku_match = _SKU_RE.search(text)
    if sku_match:
        family = "samsung"
        brand = "Samsung"
    samsung_num = _SAMSUNG_MODEL_RE.search(text)
    if samsung_num and re.search(r"\bultra\b", text, re.IGNORECASE):
        family = "samsung"
        brand = "Samsung"
        number = samsung_num.group(1)
        return brand, family, number, "ultra"

    if not re.search(r"\b(?:pro|max|plus|air|ultra|mini|se)\b", text, re.IGNORECASE):
        return None, None, None, None

    number_match = _MODEL_NUMBER_RE.search(text)
    if not number_match:
        return None, None, None, None
    number = number_match.group(1)
    _, _, variant = _parse_variant(text, "iPhone")
    return None, family, number, variant


def _build_display_model(brand: str | None, number: str | None, variant: str | None) -> str | None:
    if brand is None:
        return None
    parts: list[str] = [brand]
    if number:
        parts.append(number)
    if variant and variant not in {"base", "e"}:
        if variant == "pro max":
            parts.extend(["Pro", "Max"])
        elif variant == "pro":
            parts.append("Pro")
        elif variant == "max":
            parts.append("Max")
        elif variant == "plus":
            parts.append("Plus")
        elif variant == "air":
            parts.append("Air")
        elif variant == "ultra":
            parts.append("Ultra")
        elif variant == "mini":
            parts.append("mini")
        elif variant == "se":
            parts.append("SE")
    elif variant == "e" and number:
        parts[-1] = f"{number}e"
    return " ".join(parts).strip() or None


def extract_product_attrs(source_text: str) -> ProductAttrs:
    """Extract structured attrs from free text (employee request or supplier reply snippet)."""
    raw = (source_text or "").strip()
    text = normalize_product_text(source_text)
    if not text:
        return ProductAttrs(
            brand=None,
            family=None,
            number=None,
            variant=None,
            storage=None,
            color=None,
            region=None,
            sim=None,
            qty=None,
            condition=None,
            model=None,
            sku=None,
        )

    brand, tail = _detect_brand(text)
    family: str | None = None
    number: str | None = None
    variant: str | None = None
    sku: str | None = None
    sku_match = _SKU_RE.search(raw)
    if sku_match:
        sku = sku_match.group(1).upper()

    if brand:
        family, number, variant = _parse_variant(tail, brand)
    else:
        bl_brand, bl_family, bl_number, bl_variant = _parse_brandless_apple_like(text)
        brand = bl_brand
        family = bl_family
        number = bl_number
        variant = bl_variant

    model = _build_display_model(brand, number, variant)
    if model is None and number and variant:
        parts = [number]
        if variant == "pro max":
            parts.extend(["Pro", "Max"])
        elif variant == "pro":
            parts.append("Pro")
        elif variant == "max":
            parts.append("Max")
        elif variant == "plus":
            parts.append("Plus")
        elif variant == "ultra":
            parts.append("Ultra")
        model = " ".join(parts)

    return ProductAttrs(
        brand=brand,
        family=family,
        number=number,
        variant=variant,
        storage=_extract_storage(raw) or _extract_storage(text),
        color=_extract_color(raw) or _extract_color(text),
        region=_extract_region(raw) or _extract_region(text),
        sim=_extract_sim(raw) or _extract_sim(text),
        qty=_extract_qty(raw) or _extract_qty(text),
        condition=extract_condition(raw),
        model=model,
        sku=sku,
    )


def compute_request_confidence(attrs: ProductAttrs) -> float:
    if attrs.model and attrs.storage:
        return 0.9
    if attrs.model and (attrs.color is not None or attrs.qty is not None):
        return 0.8
    if attrs.model:
        return 0.6
    return 0.0


def attrs_match_score(
    message_attrs: ProductAttrs,
    request_normalized: dict[str, Any] | None,
) -> int:
    """
    Score how well supplier message attrs match a request normalized_json.

    Mismatch on family/number/variant -> -1 (exclude).
    """
    if not request_normalized:
        return 0

    req_attrs = extract_product_attrs(
        " ".join(
            filter(
                None,
                [
                    request_normalized.get("model") or "",
                    request_normalized.get("storage") or "",
                    request_normalized.get("color") or "",
                ],
            )
        )
    )
    if not message_attrs.model and not req_attrs.model:
        if not message_attrs.number and not req_attrs.number:
            return 0

    if message_attrs.family and req_attrs.family:
        if message_attrs.family != req_attrs.family:
            return -1
    if message_attrs.number and req_attrs.number:
        if message_attrs.number != req_attrs.number:
            return -1
    if message_attrs.variant and req_attrs.variant:
        if message_attrs.variant != req_attrs.variant:
            return -1

    score = 0
    if message_attrs.storage and req_attrs.storage:
        if message_attrs.storage.lower() == req_attrs.storage.lower():
            score += 2
    if message_attrs.color and req_attrs.color:
        if message_attrs.color.lower() == req_attrs.color.lower():
            score += 1
    if message_attrs.sim and req_attrs.sim:
        if message_attrs.sim.lower() == req_attrs.sim.lower():
            score += 1
    if message_attrs.region and req_attrs.region:
        if message_attrs.region.upper() == req_attrs.region.upper():
            score += 1
    if message_attrs.qty is not None and req_attrs.qty is not None:
        if message_attrs.qty == req_attrs.qty:
            score += 1

    if score == 0 and message_attrs.model and req_attrs.model:
        msg_norm = normalize_product_text(message_attrs.model)
        req_norm = normalize_product_text(req_attrs.model)
        if msg_norm in req_norm or req_norm in msg_norm:
            score += 2

    return score


def detect_product_contradictions(
    message_attrs: ProductAttrs,
    request_normalized: dict[str, Any] | None,
) -> list[str]:
    """Hard mismatches that block automatic single-candidate binding."""
    if not request_normalized:
        return []
    req_attrs = extract_product_attrs(
        " ".join(
            filter(
                None,
                [
                    request_normalized.get("model") or "",
                    request_normalized.get("storage") or "",
                    request_normalized.get("color") or "",
                    request_normalized.get("sim") or "",
                    request_normalized.get("region") or "",
                ],
            )
        )
    )
    conflicts: list[str] = []
    if message_attrs.number and req_attrs.number and message_attrs.number != req_attrs.number:
        conflicts.append("model_number")
    if message_attrs.variant and req_attrs.variant and message_attrs.variant != req_attrs.variant:
        conflicts.append("variant")
    if message_attrs.storage and req_attrs.storage:
        if message_attrs.storage.lower() != req_attrs.storage.lower():
            conflicts.append("storage")
    if message_attrs.color and req_attrs.color:
        if message_attrs.color.lower() != req_attrs.color.lower():
            conflicts.append("color")
    if message_attrs.sim and req_attrs.sim:
        if message_attrs.sim.lower() != req_attrs.sim.lower():
            conflicts.append("sim")
    if message_attrs.region and req_attrs.region:
        if message_attrs.region.upper() != req_attrs.region.upper():
            conflicts.append("region")
    if message_attrs.sku and req_attrs.sku and message_attrs.sku != req_attrs.sku:
        conflicts.append("sku")
    return conflicts


def attrs_compatible_for_single_candidate(
    message_attrs: ProductAttrs,
    request_normalized: dict[str, Any] | None,
) -> bool:
    return len(detect_product_contradictions(message_attrs, request_normalized)) == 0
