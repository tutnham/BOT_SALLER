"""Deterministic markup resolution (TECH DOC §9.5 step 4)."""

from __future__ import annotations

import re
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MarkupRule

# (category, markup_fixed ₽) — most specific categories first in classify_category.
SEED_MARKUP_RULES: tuple[tuple[str, str], ...] = (
    ("iphone_17_pro", "800"),
    ("iphone", "500"),
    ("airpods", "500"),
    ("apple_watch", "500"),
    ("ipad", "500"),
    ("macbook", "800"),
    ("samsung_s26_ultra", "800"),
    ("samsung", "500"),
    ("playstation", "800"),
    ("dyson", "800"),
    ("*", "500"),
)

_IPHONE_RE = re.compile(r"iphone|айфон", re.IGNORECASE)
_IPHONE_17_PRO_RE = re.compile(
    r"(?:iphone|айфон).{0,40}17.{0,20}"
    r"(?:pro\s*max|promax|pro|про\s*макс|промакс|\bпро\b)",
    re.IGNORECASE,
)
_S26_ULTRA_RE = re.compile(
    r"(?:s|с)\s*26\s*(?:ultra|ультра)",
    re.IGNORECASE,
)
_AIRPODS_RE = re.compile(r"air\s*pods?|эирподс", re.IGNORECASE)
_WATCH_RE = re.compile(
    r"apple\s*watch|iwatch|эппл\s*вотч|watch\s*(?:ultra|se|series)",
    re.IGNORECASE,
)
_IPAD_RE = re.compile(r"ipad|айпад", re.IGNORECASE)
_MACBOOK_RE = re.compile(r"mac\s*book|макбук", re.IGNORECASE)
_SAMSUNG_RE = re.compile(r"samsung|galaxy|самсунг", re.IGNORECASE)
_PLAYSTATION_RE = re.compile(
    r"playstation|play\s*station|ps5|ps4|dualsense|плейстейш|пс5|пс4",
    re.IGNORECASE,
)
_DYSON_RE = re.compile(r"dyson|дайсон", re.IGNORECASE)


def classify_category(model: str | None) -> str:
    """Map model string to a markup_rules.category key."""
    text = (model or "").strip().lower()
    if not text:
        return "*"
    if _IPHONE_RE.search(text) and _IPHONE_17_PRO_RE.search(text):
        return "iphone_17_pro"
    if _IPHONE_RE.search(text):
        return "iphone"
    if _S26_ULTRA_RE.search(text):
        return "samsung_s26_ultra"
    if _SAMSUNG_RE.search(text):
        return "samsung"
    if _MACBOOK_RE.search(text):
        return "macbook"
    if _AIRPODS_RE.search(text):
        return "airpods"
    if _WATCH_RE.search(text) and "galaxy" not in text:
        return "apple_watch"
    if _IPAD_RE.search(text):
        return "ipad"
    if _PLAYSTATION_RE.search(text):
        return "playstation"
    if _DYSON_RE.search(text):
        return "dyson"
    return "*"


def seed_markup_rule_rows() -> list[MarkupRule]:
    """Active fixed-ruble rules matching classify_category keys."""
    return [
        MarkupRule(
            category=category,
            markup_fixed=Decimal(amount),
            active=True,
        )
        for category, amount in SEED_MARKUP_RULES
    ]


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
