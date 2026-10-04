"""Deterministic product category classification for routing."""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ProductCategory
from app.parsers.product_normalizer import extract_product_attrs, normalize_product_text

_POWER_STATION_RE = re.compile(
    r"(?:power\s*station|электростанц|ecoflow|bluetti|jackery|anker\s*solix)",
    re.IGNORECASE,
)
_SAMSUNG_RE = re.compile(r"(?:samsung|galaxy|самсунг)", re.IGNORECASE)
_APPLE_RE = re.compile(
    r"(?:iphone|ipad|macbook|airpods|apple\s*watch|iwatch|айфон|айпад|макбук)",
    re.IGNORECASE,
)
_OTHER_BRANDS_RE = re.compile(
    r"(?:xiaomi|redmi|pixel|huawei|honor|dyson|playstation|ps5|jbl|sony|lg\b)",
    re.IGNORECASE,
)

VALID_CATEGORIES = frozenset(
    {
        ProductCategory.apple.value,
        ProductCategory.samsung.value,
        ProductCategory.power_station.value,
        ProductCategory.other.value,
        "unknown",
    }
)


def classify_product_deterministic(text: str) -> str | None:
    """Return category or None when unknown (needs LLM)."""
    normalized = normalize_product_text(text)
    if not normalized:
        return None
    if _POWER_STATION_RE.search(normalized):
        return ProductCategory.power_station.value
    if _SAMSUNG_RE.search(normalized):
        return ProductCategory.samsung.value
    if _APPLE_RE.search(normalized):
        return ProductCategory.apple.value
    if _OTHER_BRANDS_RE.search(normalized):
        return ProductCategory.other.value
    attrs = extract_product_attrs(text)
    if attrs.brand == "Samsung":
        return ProductCategory.samsung.value
    if attrs.brand in {"iPhone", "iPad", "MacBook", "AirPods", "Apple Watch"}:
        return ProductCategory.apple.value
    return None


def split_positions(source_text: str) -> list[str]:
    """Split multi-line or semicolon-separated order into position segments."""
    raw = (source_text or "").strip()
    if not raw:
        return []
    parts: list[str] = []
    for line in raw.replace(";", "\n").splitlines():
        segment = line.strip()
        if segment:
            parts.append(segment)
    if len(parts) <= 1:
        return [raw]

    product_segments = [
        segment
        for segment in parts
        if classify_product_deterministic(segment) is not None
        or extract_product_attrs(segment).model
    ]
    if len(product_segments) >= 2:
        return product_segments
    return [raw]


async def classify_product_with_llm(
    session: AsyncSession,
    text: str,
) -> tuple[str, float]:
    """LLM fallback for unknown products. Returns (category, confidence)."""
    from app.config import get_settings
    from app.llm.client import LLMProviderError, get_llm_client
    from app.llm.schemas import validate_product_classification
    from app.parsers.cache import get_cached, set_cached

    kind = "classify_product"
    cached = await get_cached(session, kind=kind, raw_text=text)
    if cached is not None:
        parsed = validate_product_classification(cached)
        return parsed.category, parsed.confidence

    llm = get_llm_client()
    try:
        parsed = await llm.classify_product(text)
    except LLMProviderError:
        return "unknown", 0.0

    settings = get_settings()
    await set_cached(
        session,
        kind=kind,
        raw_text=text,
        result_json=parsed.model_dump(mode="json"),
        model_used=settings.llm_model or settings.llm_provider,
    )
    return parsed.category, parsed.confidence


async def resolve_product_category(session: AsyncSession, text: str) -> str:
    """Full pipeline: regex first, LLM for unknown."""
    from app.config import get_settings

    deterministic = classify_product_deterministic(text)
    if deterministic is not None:
        return deterministic

    category, confidence = await classify_product_with_llm(session, text)
    threshold = get_settings().confidence_threshold
    if confidence >= threshold and category in VALID_CATEGORIES and category != "unknown":
        return category
    return "unknown"


def category_from_normalized(normalized_json: dict[str, Any] | None) -> str | None:
    if not normalized_json:
        return None
    stored = normalized_json.get("category")
    if isinstance(stored, str) and stored:
        return stored
    model = normalized_json.get("model") or ""
    return classify_product_deterministic(str(model))
