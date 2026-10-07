"""Markup rules: iPhone 18 line and owner-created rules."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.markup_service import (
    create_markup_rule,
    get_markup,
    phrase_to_model_pattern,
    seed_markup_rule_rows,
)


def test_iphone_18_plus_gets_800_markup() -> None:
    rules = seed_markup_rule_rows()
    result = get_markup("iPhone 18 Plus 256GB", rules)
    assert result.rule_key == "iphone_18_plus"
    assert result.markup == Decimal("800")


def test_phrase_to_model_pattern_normalized() -> None:
    pattern = phrase_to_model_pattern("iPhone 18 Plus")
    assert pattern == r"iphone\s+18\s+plus"


@pytest.mark.asyncio
async def test_create_markup_rule_persists(db_session: AsyncSession) -> None:
    rule = await create_markup_rule(
        db_session,
        phrase="Xiaomi 15 Ultra",
        markup_rub=Decimal("600"),
    )
    assert rule.id is not None
    assert rule.markup_fixed == Decimal("600")
    rules = seed_markup_rule_rows()
    rules.append(rule)
    result = get_markup("Xiaomi 15 Ultra 512", rules)
    assert result.markup == Decimal("600")
