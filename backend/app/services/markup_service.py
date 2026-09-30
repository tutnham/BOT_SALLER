"""Deterministic markup resolution (TECH DOC §9.5 step 4)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import MarkupRule
from app.parsers.product_normalizer import normalize_product_text

CUSTOMER_MARKUP_RULES: tuple[tuple[str, str, str, str, int], ...] = (
    ("iphone_17_pro_max", "apple", r"iphone\s+17\s+pro\s+max", "800", 100),
    ("iphone_17_pro", "apple", r"iphone\s+17\s+pro(?:\s|$)", "800", 99),
    ("iphone_17_air", "apple", r"iphone\s+17\s+air", "500", 98),
    ("iphone_17_e", "apple", r"iphone\s+17\s*e(?:\s|$)", "500", 97),
    ("iphone_17_base", "apple", r"iphone\s+17(?:\s|$)", "500", 96),
    ("iphone_16_pro_max", "apple", r"iphone\s+16\s+pro\s+max", "800", 95),
    (
        "iphone_13_16",
        "apple",
        r"iphone\s+(?:1[3-6])(?:\s|$|\s+(?:pro|max|plus|mini|se|e)\b)",
        "500",
        90,
    ),
    ("ipad", "apple", r"\bipad\b", "500", 80),
    ("apple_watch", "apple", r"(?:apple\s*watch|iwatch)\b", "500", 79),
    ("airpods", "apple", r"\bairpods?\b", "500", 78),
    ("macbook", "apple", r"\bmacbook\b", "1000", 77),
    ("samsung", "samsung", r"(?:samsung|galaxy)\b", "800", 70),
)


@dataclass(frozen=True)
class MarkupResult:
    markup: Decimal
    rule_id: int | None
    rule_key: str | None
    is_fallback: bool


def seed_markup_rule_rows() -> list[MarkupRule]:
    """Active pattern-based rules for tests and demo seed."""
    return [
        MarkupRule(
            category=rule_key,
            rule_key=rule_key,
            brand=brand,
            model_pattern=pattern,
            markup_fixed=Decimal(amount),
            priority=priority,
            active=True,
        )
        for rule_key, brand, pattern, amount, priority in CUSTOMER_MARKUP_RULES
    ]


async def load_rules(session: AsyncSession) -> list[MarkupRule]:
    """Load active v2 rules sorted by priority descending."""
    result = await session.execute(
        select(MarkupRule)
        .where(
            MarkupRule.active.is_(True),
            MarkupRule.model_pattern.isnot(None),
        )
        .order_by(MarkupRule.priority.desc().nullslast(), MarkupRule.id.asc())
    )
    return list(result.scalars().all())


@lru_cache(maxsize=128)
def _compile_pattern(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE)


def _product_search_text(product_name_or_attrs: str | dict | None) -> str:
    if product_name_or_attrs is None:
        return ""
    if isinstance(product_name_or_attrs, dict):
        model = product_name_or_attrs.get("model") or ""
        storage = product_name_or_attrs.get("storage") or ""
        return normalize_product_text(f"{model} {storage}".strip())
    return normalize_product_text(str(product_name_or_attrs))


def get_markup(
    product_name_or_attrs: str | dict | None,
    rules: list[MarkupRule],
    *,
    default: Decimal | None = None,
) -> MarkupResult:
    """
    Resolve fixed markup for a product name or normalized_json dict.

    Falls back to ``default_markup`` from settings when no rule matches.
    """
    if default is None:
        default = get_settings().default_markup

    text = _product_search_text(product_name_or_attrs)
    if text:
        for rule in rules:
            pattern = rule.model_pattern
            if not pattern:
                continue
            if _compile_pattern(pattern).search(text):
                fixed = rule.markup_fixed
                if fixed is not None:
                    return MarkupResult(
                        markup=Decimal(fixed),
                        rule_id=rule.id,
                        rule_key=rule.rule_key,
                        is_fallback=False,
                    )

    logger.warning(
        "Markup fallback for product={!r} default={}",
        product_name_or_attrs,
        default,
    )
    return MarkupResult(
        markup=default,
        rule_id=None,
        rule_key=None,
        is_fallback=True,
    )


def apply_markup(
    price: Decimal,
    product_name_or_attrs: str | dict | None,
    rules: list[MarkupRule],
    *,
    default: Decimal | None = None,
) -> tuple[Decimal, Decimal, int | None, str | None]:
    """Return (final_price, markup_rub, rule_id, rule_key)."""
    result = get_markup(product_name_or_attrs, rules, default=default)
    final = price + result.markup
    return final, result.markup, result.rule_id, result.rule_key


async def update_rule_markup(
    session: AsyncSession,
    rule_key: str,
    markup_rub: Decimal,
) -> MarkupRule | None:
    """Owner/admin: change fixed markup for a rule_key."""
    result = await session.execute(
        select(MarkupRule).where(MarkupRule.rule_key == rule_key)
    )
    rule = result.scalar_one_or_none()
    if rule is None:
        return None
    rule.markup_fixed = markup_rub
    await session.flush()
    return rule


async def list_active_rules(session: AsyncSession) -> list[MarkupRule]:
    return await load_rules(session)
