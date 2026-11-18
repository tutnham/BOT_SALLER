"""Markup rules: iPhone 18 line and owner-created rules."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MarkupRule
from app.services.markup_service import (
    create_markup_rule,
    decline_unpriced_markup_rule,
    ensure_markup_rule_for_product,
    get_markup,
    phrase_to_model_pattern,
    seed_markup_rule_rows,
)
from app.services.price_service import notify_unknown_markup_rules


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


@pytest.mark.asyncio
async def test_unknown_product_prompt_offers_markup_or_skip() -> None:
    from tests.conftest import MockTelegramClient

    telegram = MockTelegramClient()
    rule = MarkupRule(
        id=7,
        category="custom_pixel_9",
        rule_key="custom_pixel_9",
        brand="*",
        model_pattern=r"pixel\s+9",
        markup_fixed=None,
        priority=60,
        active=True,
    )
    await notify_unknown_markup_rules(telegram, chat_ids=[42], rules=[rule])
    assert len(telegram.sent) == 1
    _chat_id, text, markup = telegram.sent[0]
    assert "новая позиция" in text
    assert "Не добавлять" in text
    labels = [
        button.get("text")
        for row in (markup or {}).get("inline_keyboard", [])
        for button in row
    ]
    assert "500" in labels
    assert "Не добавлять" in labels


@pytest.mark.asyncio
async def test_decline_unpriced_markup_rule_deactivates(db_session: AsyncSession) -> None:
    rule = await ensure_markup_rule_for_product(db_session, phrase="Nothing Phone 3")
    assert rule.markup_fixed is None

    outcome = await decline_unpriced_markup_rule(db_session, int(rule.id))
    assert outcome == "declined"
    await db_session.refresh(rule)
    assert rule.active is False

    again = await decline_unpriced_markup_rule(db_session, int(rule.id))
    assert again == "declined"
