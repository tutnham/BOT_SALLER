"""Deterministic markup resolution (TECH DOC §9.5 step 4)."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MarkupRule

_APPLE_MARKERS = (
    "iphone",
    "ipad",
    "macbook",
    "airpods",
    "apple watch",
    "imac",
    "mac mini",
    "mac studio",
)
_SAMSUNG_MARKERS = ("samsung", "galaxy", "galaxy tab")
_PLAYSTATION_MARKERS = ("playstation", "ps5", "ps4", "dualsense")


def classify_category(model: str | None) -> str:
    """Map model string to markup category: apple / samsung / playstation / *."""
    text = (model or "").strip().lower()
    if not text:
        return "*"
    if any(marker in text for marker in _APPLE_MARKERS):
        return "apple"
    if any(marker in text for marker in _SAMSUNG_MARKERS):
        return "samsung"
    if any(marker in text for marker in _PLAYSTATION_MARKERS):
        return "playstation"
    return "*"


async def load_rules(session: AsyncSession) -> dict[str, MarkupRule]:
    """Load active markup rules keyed by category (one query)."""
    result = await session.execute(
        select(MarkupRule).where(MarkupRule.active.is_(True))
    )
    rules: dict[str, MarkupRule] = {}
    for rule in result.scalars().all():
        rules[rule.category] = rule
    return rules


def _clamp(value: Decimal, lo: Decimal | None, hi: Decimal | None) -> Decimal:
    if lo is not None and value < lo:
        value = lo
    if hi is not None and value > hi:
        value = hi
    return value


def resolve_markup(
    rules: dict[str, MarkupRule],
    category: str,
    default: Decimal,
) -> Decimal | None:
    """
    Resolve markup for category with fallback to ``*``.

    Returns ``None`` when neither category nor ``*`` rule exists.
    """
    rule = rules.get(category) or rules.get("*")
    if rule is None:
        return None

    if rule.markup_fixed is not None:
        return Decimal(rule.markup_fixed)

    return _clamp(default, rule.markup_min, rule.markup_max)
