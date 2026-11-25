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


def test_brandless_18_pro_uses_family_markup() -> None:
    rules = seed_markup_rule_rows()
    result = get_markup("18 pro 512gb", rules)
    assert result.rule_key == "iphone_18_pro"
    assert result.markup == Decimal("800")
    assert result.is_fallback is False


def test_iphone_13_air_is_not_family_markup() -> None:
    rules = seed_markup_rule_rows()
    air_13 = get_markup("iPhone 13 Air 256GB", rules)
    assert air_13.is_fallback is True
    assert air_13.rule_key is None
    assert get_markup("iPhone 15 128GB", rules).rule_key == "iphone_13_16"
    assert get_markup("iPhone 16 Pro", rules).rule_key == "iphone_13_16"
    assert get_markup("iPhone 17 Air 256GB", rules).rule_key == "iphone_17_air"


def test_markup_buttons_group_models_and_hide_custom_duplicate() -> None:
    from app.services.markup_service import (
        markup_rule_label,
        model_sort_key,
        representative_rules,
    )

    rules = seed_markup_rule_rows()
    for index, rule in enumerate(rules, start=1):
        rule.id = index
    duo = MarkupRule(
        id=50,
        category="custom_iphone_duo",
        rule_key="custom_iphone_duo",
        brand="apple",
        model_pattern=r"iphone\s+duo",
        markup_fixed=Decimal("1000"),
        priority=60,
        active=True,
    )
    duplicate = MarkupRule(
        id=51,
        category="custom_18_pro_512gb",
        rule_key="custom_18_pro_512gb",
        brand="*",
        model_pattern=r"18\s+pro\s+512gb",
        markup_fixed=None,
        priority=60,
        active=True,
    )
    legacy_priced = MarkupRule(
        id=52,
        category="custom_iphone_16_256gb",
        rule_key="custom_iphone_16_256gb",
        brand="*",
        model_pattern=r"iphone\ 16\ 256gb",
        markup_fixed=Decimal("700"),
        priority=60,
        active=True,
    )
    all_rules = sorted(
        [*rules, duo, duplicate, legacy_priced],
        key=lambda rule: -(rule.priority or 0),
    )
    reps = representative_rules(all_rules)
    rule_ids = [int(rule.id) for rule in reps]
    assert len(rule_ids) == len(set(rule_ids))
    iphone = sorted(
        (rule for rule in reps if (rule.rule_key or "").startswith(("iphone_", "custom_iphone"))),
        key=model_sort_key,
    )
    labels = [markup_rule_label(rule) for rule in iphone]
    assert labels[0] == "iPhone Duo"
    assert labels[-1] == "iPhone 13–16"
    assert labels.count("iPhone 18 Pro") == 1
    assert "custom" not in " ".join(labels)
    family = get_markup("iPhone 16 256GB", all_rules)
    assert family.rule_key == "iphone_13_16"


@pytest.mark.asyncio
async def test_unknown_18_pro_does_not_create_custom_rule(
    db_session: AsyncSession,
    seed_markup_rules: list[MarkupRule],
) -> None:
    from sqlalchemy import select

    from app.services.price_service import collect_unknown_markup_products

    created = await collect_unknown_markup_products(
        db_session,
        [{"model": "18 pro", "storage": "512GB", "color": "Black"}],
        seed_markup_rules,
    )
    assert created == []
    custom = await db_session.scalar(
        select(MarkupRule).where(MarkupRule.rule_key.like("custom_%"))
    )
    assert custom is None


@pytest.mark.asyncio
async def test_empty_custom_shadowed_by_family_is_retired(
    db_session: AsyncSession,
    seed_markup_rules: list[MarkupRule],
) -> None:
    from app.services.markup_service import retire_shadowed_custom_rules

    custom = MarkupRule(
        category="custom_18_pro_256gb",
        rule_key="custom_18_pro_256gb",
        brand="*",
        model_pattern=r"18\s+pro\s+256gb",
        markup_fixed=None,
        priority=60,
        active=True,
    )
    db_session.add(custom)
    await db_session.flush()

    retired = await retire_shadowed_custom_rules(db_session)
    assert retired == 1
    await db_session.refresh(custom)
    assert custom.active is False
    assert seed_markup_rules


@pytest.mark.asyncio
async def test_markup_menu_is_categories_then_model_prices(
    db_session: AsyncSession,
    seed_markup_rules: list[MarkupRule],
    seed_suppliers: list,
) -> None:
    from datetime import UTC, datetime
    from decimal import Decimal

    from app.db.models import ParsedItem, RawPrice
    from app.handlers.owner_commands import (
        send_markup_category,
        send_markup_edit,
        send_markup_list,
    )
    from tests.conftest import MockTelegramClient

    raw = RawPrice(
        supplier_id=seed_suppliers[0].id,
        text="18 pro 512 black",
        content_hash="markup-menu-18-pro",
        source="channel_post",
        received_at=datetime.now(UTC),
    )
    db_session.add(raw)
    await db_session.flush()
    db_session.add(
        ParsedItem(
            raw_price_id=raw.id,
            sku_key="18-pro-512-black",
            model="18 pro",
            storage="512GB",
            color="Black",
            region="US",
            price=Decimal("61000"),
            currency="RUB",
        )
    )
    await db_session.flush()

    telegram = MockTelegramClient()
    await send_markup_list(db_session, chat_id=1, telegram=telegram)
    text, markup = telegram.sent[-1][1], telegram.sent[-1][2]
    assert text == "Наценки. Выберите категорию."
    assert "iphone 18 pro max:" not in text
    labels = [
        button.get("text")
        for row in (markup or {}).get("inline_keyboard", [])
        for button in row
    ]
    assert "iPhone" in labels
    assert "MacBook" in labels
    assert "Samsung" in labels
    assert all("custom" not in label.casefold() for label in labels)

    telegram.sent.clear()
    await send_markup_category(
        db_session, chat_id=1, telegram=telegram, category_arg=1, message_id=7
    )
    _chat, _message_id, category_text, category_markup = telegram.edited[-1]
    assert "Выберите модель" in category_text
    model_labels = [
        button.get("text")
        for row in (category_markup or {}).get("inline_keyboard", [])
        for button in row
    ]
    assert model_labels[0].startswith("iPhone 18 Pro Max")
    assert "iPhone 18 Pro ·" in " ".join(model_labels)
    assert all("custom" not in label.casefold() for label in model_labels)

    telegram.edited.clear()
    await send_markup_category(
        db_session, chat_id=1, telegram=telegram, category_arg=1, page=1, message_id=7
    )
    last_labels = [
        button.get("text")
        for row in (telegram.edited[-1][3] or {}).get("inline_keyboard", [])
        for button in row
    ]
    assert any(label.startswith("iPhone 13–16") for label in last_labels)

    pro = next(rule for rule in seed_markup_rules if rule.rule_key == "iphone_18_pro")
    telegram.edited.clear()
    await send_markup_edit(
        db_session, chat_id=1, rule_id=int(pro.id), telegram=telegram, message_id=7
    )
    model_text = telegram.edited[-1][2]
    assert "iPhone 18 Pro — 800 ₽" in model_text
    assert "512GB Black US — 61 000 ₽" in model_text
    assert seed_suppliers
