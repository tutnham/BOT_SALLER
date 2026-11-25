"""Deterministic markup resolution (TECH DOC §9.5 step 4)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from functools import lru_cache

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import MarkupRule
from app.parsers.product_normalizer import (
    canonicalize_catalog_fields,
    normalize_product_text,
)

CUSTOMER_MARKUP_RULES: tuple[tuple[str, str, str, str, int], ...] = (
    ("iphone_18_pro_max", "apple", r"iphone\s+18\s+pro\s+max", "800", 105),
    ("iphone_18_pro", "apple", r"iphone\s+18\s+pro(?:\s|$)", "800", 104),
    ("iphone_18_plus", "apple", r"iphone\s+18\s+plus", "800", 103),
    ("iphone_18_base", "apple", r"iphone\s+18(?:\s|$)", "500", 102),
    ("iphone_17_pro_max", "apple", r"iphone\s+17\s+pro\s+max", "800", 100),
    ("iphone_17_pro", "apple", r"iphone\s+17\s+pro(?:\s|$)", "800", 99),
    ("iphone_17_air", "apple", r"iphone\s+17\s+air", "500", 98),
    ("iphone_17_e", "apple", r"iphone\s+17\s*e(?:\s|$)", "500", 97),
    ("iphone_17_base", "apple", r"iphone\s+17(?:\s|$)", "500", 96),
    ("iphone_16_pro_max", "apple", r"iphone\s+16\s+pro\s+max", "800", 95),
    (
        "iphone_13_16",
        "apple",
        # No iPhone 13–16 Air exists; «air» after 13–16 is a MacBook/iPad line.
        r"iphone\s+(?:1[3-6])(?:\s+(?!air\b)|$)",
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


_rules_cache: tuple[list[MarkupRule], datetime] | None = None
_RULES_CACHE_TTL_SECONDS = 60


def invalidate_markup_rules_cache() -> None:
    global _rules_cache
    _rules_cache = None


async def load_rules(session: AsyncSession) -> list[MarkupRule]:
    """Load active v2 rules sorted by priority descending."""
    global _rules_cache
    now = datetime.now(UTC)
    if _rules_cache is not None:
        cached_rules, cached_at = _rules_cache
        if (now - cached_at).total_seconds() < _RULES_CACHE_TTL_SECONDS:
            return cached_rules

    result = await session.execute(
        select(MarkupRule)
        .where(
            MarkupRule.active.is_(True),
            MarkupRule.model_pattern.isnot(None),
        )
        .order_by(MarkupRule.priority.desc().nullslast(), MarkupRule.id.asc())
    )
    rules = list(result.scalars().all())
    _rules_cache = (rules, now)
    return rules


@lru_cache(maxsize=128)
def _compile_pattern(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE)


def _product_search_text(product_name_or_attrs: str | dict | None) -> str:
    if product_name_or_attrs is None:
        return ""
    if isinstance(product_name_or_attrs, dict):
        model = str(product_name_or_attrs.get("model") or "")
        storage = str(product_name_or_attrs.get("storage") or "")
    else:
        model = str(product_name_or_attrs)
        storage = ""
    canonical, parsed_storage = canonicalize_catalog_fields(model, storage or None)
    return normalize_product_text(f"{canonical or ''} {parsed_storage or ''}".strip())


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
    return await update_rule_markup_by_id(session, rule.id, markup_rub)


async def decline_unpriced_markup_rule(
    session: AsyncSession,
    rule_id: int,
) -> str:
    """Deactivate a new product rule that still has no markup.

    Returns ``declined``, ``missing``, or ``already_priced``.
    """
    rule = await session.get(MarkupRule, rule_id)
    if rule is None:
        return "missing"
    if rule.markup_fixed is not None:
        return "already_priced"
    rule.active = False
    rule.updated_at = datetime.now(UTC)
    invalidate_markup_rules_cache()
    await session.flush()
    return "declined"


async def update_rule_markup_by_id(
    session: AsyncSession,
    rule_id: int,
    markup_rub: Decimal,
) -> MarkupRule | None:
    rule = await session.get(MarkupRule, rule_id)
    if rule is None:
        return None
    rule.markup_fixed = markup_rub
    rule.updated_at = datetime.now(UTC)
    invalidate_markup_rules_cache()
    await session.flush()
    return rule


async def list_active_rules(session: AsyncSession) -> list[MarkupRule]:
    return await load_rules(session)


def phrase_to_model_pattern(phrase: str) -> str:
    """Build a regex for the model identity, without storage or color."""
    text = _markup_identity_text(phrase)
    if not text:
        raise ValueError("empty_phrase")
    tokens = text.split()
    return r"\s+".join(re.escape(token) for token in tokens)


def slug_rule_key(phrase: str) -> str:
    text = _markup_identity_text(phrase).replace(" ", "_")
    if not text:
        raise ValueError("empty_phrase")
    key = f"custom_{text}"
    return key[:120]


def _markup_identity_text(phrase: str) -> str:
    model, _storage = canonicalize_catalog_fields(phrase, None)
    return normalize_product_text(model or phrase)


async def ensure_markup_rule_for_product(
    session: AsyncSession,
    *,
    phrase: str,
    brand: str | None = None,
) -> MarkupRule:
    """Create an active rule for a parsed product if missing. Markup may be empty."""
    pattern = phrase_to_model_pattern(phrase)
    rule_key = slug_rule_key(phrase)
    existing = await session.scalar(
        select(MarkupRule).where(MarkupRule.rule_key == rule_key)
    )
    if existing is not None:
        if not existing.model_pattern:
            existing.model_pattern = pattern
            invalidate_markup_rules_cache()
            await session.flush()
        return existing

    rule = MarkupRule(
        category=rule_key,
        rule_key=rule_key,
        brand=brand or "*",
        model_pattern=pattern,
        markup_fixed=None,
        priority=60,
        active=True,
    )
    session.add(rule)
    invalidate_markup_rules_cache()
    await session.flush()
    return rule


async def create_markup_rule(
    session: AsyncSession,
    *,
    phrase: str,
    markup_rub: Decimal,
    priority: int = 50,
    brand: str | None = None,
) -> MarkupRule:
    """Owner-defined product phrase + fixed markup (no code deploy)."""
    pattern = phrase_to_model_pattern(phrase)
    rule_key = slug_rule_key(phrase)
    existing = await session.scalar(
        select(MarkupRule).where(MarkupRule.rule_key == rule_key)
    )
    if existing is not None:
        existing.model_pattern = pattern
        existing.markup_fixed = markup_rub
        existing.priority = priority
        existing.active = True
        if brand:
            existing.brand = brand
        existing.updated_at = datetime.now(UTC)
        invalidate_markup_rules_cache()
        await session.flush()
        return existing

    rule = MarkupRule(
        category=rule_key,
        rule_key=rule_key,
        brand=brand or "*",
        model_pattern=pattern,
        markup_fixed=markup_rub,
        priority=priority,
        active=True,
    )
    session.add(rule)
    invalidate_markup_rules_cache()
    await session.flush()
    return rule


MARKUP_CATEGORIES: tuple[tuple[int, str, str], ...] = (
    (1, "iPhone", "iphone"),
    (2, "iPad", "ipad"),
    (3, "Apple Watch", "apple_watch"),
    (4, "AirPods", "airpods"),
    (5, "MacBook", "macbook"),
    (6, "Samsung", "samsung"),
    (7, "Прочее", "other"),
)

_RULE_LABELS = {
    "iphone_13_16": "iPhone 13–16",
    "iphone_18_base": "iPhone 18",
    "iphone_17_base": "iPhone 17",
    "apple_watch": "Apple Watch",
    "airpods": "AirPods",
    "ipad": "iPad",
    "macbook": "MacBook",
    "samsung": "Samsung",
}

_VARIANT_RANK = {
    "duo": 0,
    "pro max": 1,
    "pro": 2,
    "plus": 3,
    "air": 4,
    "e": 5,
    "base": 6,
}


def markup_rule_label(rule: MarkupRule) -> str:
    """Human name without the internal custom_ prefix."""
    key = rule.rule_key or ""
    if key in _RULE_LABELS:
        return _RULE_LABELS[key]
    source = key.removeprefix("custom_").replace("_", " ").strip()
    model, _storage = canonicalize_catalog_fields(source or rule.category, None)
    if model and model.casefold() != (source or "").casefold():
        return model
    if model and model.casefold().startswith(
        ("iphone", "ipad", "macbook", "airpods", "apple watch", "samsung")
    ):
        return model
    return source or "правило"


def markup_category_code(rule: MarkupRule) -> str:
    key = rule.rule_key or ""
    if key == "iphone_13_16" or key.startswith("iphone_"):
        return "iphone"
    if key in {"ipad", "apple_watch", "airpods", "macbook", "samsung"}:
        return key
    label = markup_rule_label(rule)
    from app.parsers.product_normalizer import extract_product_attrs

    attrs = extract_product_attrs(label)
    if attrs.family == "iphone" or attrs.brand == "iPhone":
        return "iphone"
    if attrs.family == "ipad" or attrs.brand == "iPad":
        return "ipad"
    if attrs.family == "apple_watch" or attrs.brand == "Apple Watch":
        return "apple_watch"
    if attrs.family == "airpods" or attrs.brand == "AirPods":
        return "airpods"
    if attrs.family == "macbook" or attrs.brand == "MacBook":
        return "macbook"
    if attrs.family == "samsung" or attrs.brand == "Samsung" or rule.brand == "samsung":
        return "samsung"
    return "other"


def markup_category_arg(code: str) -> int:
    for arg, _label, stored in MARKUP_CATEGORIES:
        if stored == code:
            return arg
    return 7


def markup_category_label(arg: int) -> str:
    for stored_arg, label, _code in MARKUP_CATEGORIES:
        if stored_arg == arg:
            return label
    return "Прочее"


def model_sort_key(rule: MarkupRule) -> tuple:
    label = markup_rule_label(rule)
    if rule.rule_key == "iphone_13_16":
        return (2, 0, 0, label.casefold())
    from app.parsers.product_normalizer import extract_product_attrs

    attrs = extract_product_attrs(label)
    if attrs.variant == "duo" or "duo" in label.casefold():
        return (0, 0, 0, label.casefold())
    if markup_category_code(rule) == "iphone":
        number = int(attrs.number) if attrs.number and attrs.number.isdigit() else -1
        rank = _VARIANT_RANK.get(attrs.variant or "", 9)
        return (1, -number, rank, label.casefold())
    return (1, 0, 0, label.casefold())


def representative_rules(rules: list[MarkupRule]) -> list[MarkupRule]:
    """One button per model. A family rule hides storage-specific custom rows."""
    by_id = {int(rule.id): rule for rule in rules if rule.id is not None}
    chosen: dict[int, MarkupRule] = {}
    for rule in rules:
        if not rule.active:
            continue
        label = markup_rule_label(rule)
        result = get_markup(label, rules)
        winner = by_id.get(int(result.rule_id)) if result.rule_id is not None else None
        if winner is None or not winner.active:
            winner = rule
        if winner.id is None:
            continue
        chosen.setdefault(int(winner.id), winner)
    return list(chosen.values())


async def retire_shadowed_custom_rules(session: AsyncSession) -> int:
    """Deactivate empty custom rules already covered by a family markup."""
    invalidate_markup_rules_cache()
    rules = await load_rules(session)
    retired = 0
    for rule in rules:
        key = rule.rule_key or ""
        if not key.startswith("custom_") or rule.markup_fixed is not None:
            continue
        result = get_markup(markup_rule_label(rule), rules)
        if result.is_fallback or result.rule_id is None or result.rule_id == rule.id:
            continue
        rule.active = False
        rule.updated_at = datetime.now(UTC)
        retired += 1
    if retired:
        invalidate_markup_rules_cache()
        await session.flush()
    return retired
