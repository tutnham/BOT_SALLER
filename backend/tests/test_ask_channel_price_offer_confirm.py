"""Оффер цены из прайс-канала по заявке и публикация только по кнопке."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import (
    Request,
    RequestPriceSelection,
    RequestPriceState,
    RequestStatus,
    Supplier,
    TelegramOutbox,
)
from app.llm.client import set_llm_client
from app.services.channel_sku_price_service import (
    confirm_channel_price_offer,
    offer_channel_price_for_request,
    rebuild_channel_sku_prices,
)
from app.services.price_service import aggregate_today
from tests.conftest import MockLLMClient, MockTelegramClient
from tests.test_channel_sku_price_projection import (
    KEY_15,
    TODAY,
    _seed_channel_price,
)


async def _make_request(
    db_session: AsyncSession, employee_id: int, group_chat_id: int
) -> Request:
    request = Request(
        group_chat_id=group_chat_id,
        employee_id=employee_id,
        source_text="iPhone 15 256GB Black",
        canonical_sku_key=KEY_15,
        requested_qty=1,
        status=RequestStatus.open,
    )
    db_session.add(request)
    await db_session.flush()
    return request


async def _seed_offer(
    db_session: AsyncSession,
    mock_llm: MockLLMClient,
    supplier: Supplier,
    employee_id: int,
    group_chat_id: int,
    telegram: MockTelegramClient,
) -> Request:
    """Цена канала в проекции + карточка по заявке; возвращает заявку."""
    await _seed_channel_price(
        db_session, mock_llm, supplier, 100000, post_date=TODAY
    )
    await rebuild_channel_sku_prices(
        db_session, await aggregate_today(db_session), now=TODAY
    )
    request = await _make_request(db_session, employee_id, group_chat_id)
    assert not telegram.sent  # type: ignore[attr-defined]
    assert await offer_channel_price_for_request(
        db_session, request, telegram=telegram, now=TODAY
    ) is True
    return request


async def _client_outbox(db_session: AsyncSession) -> list[TelegramOutbox]:
    return list(
        (
            await db_session.scalars(
                select(TelegramOutbox).where(TelegramOutbox.kind == "client_price")
            )
        ).all()
    )


@pytest.mark.asyncio
async def test_offer_is_staff_only_and_publishes_nothing(
    db_session: AsyncSession,
    mock_telegram: MockTelegramClient,
    mock_llm: MockLLMClient,
    seed_channel_supplier: Supplier,
    seed_markup_rules,
    seed_employee,
    seed_owner,
    seed_group_chat_id: int,
    channel_price_flags_on: None,
) -> None:
    set_llm_client(mock_llm)
    try:
        request = await _seed_offer(
            db_session,
            mock_llm,
            seed_channel_supplier,
            seed_employee.id,
            seed_group_chat_id,
            mock_telegram,
        )

        card = mock_telegram.sent[-1]
        assert card[0] == seed_group_chat_id
        assert "100500" in card[1]
        assert "chanprice:confirm" in str(card[2])
        # Карточка видна только персоналу: в группе клиента нет публичных сообщений.
        assert mock_telegram.ephemeral_message_parameters
        assert all(params is not None for params in mock_telegram.ephemeral_message_parameters)
        assert await _client_outbox(db_session) == []

        offers = (
            await db_session.scalars(select(RequestPriceSelection))
        ).all()
        assert len(offers) == 1
        assert offers[0].status == "proposed"
        assert offers[0].selection_reason == "channel_price"
        assert offers[0].selected_quote_id is None
        assert offers[0].client_unit_price == Decimal("100500.00")
        assert request.price_state is None
    finally:
        set_llm_client(None)


@pytest.mark.asyncio
async def test_confirm_publishes_once_and_second_click_is_noop(
    db_session: AsyncSession,
    mock_telegram: MockTelegramClient,
    mock_llm: MockLLMClient,
    seed_channel_supplier: Supplier,
    seed_markup_rules,
    seed_employee,
    seed_owner,
    seed_group_chat_id: int,
    channel_price_flags_on: None,
) -> None:
    set_llm_client(mock_llm)
    try:
        request = await _seed_offer(
            db_session,
            mock_llm,
            seed_channel_supplier,
            seed_employee.id,
            seed_group_chat_id,
            mock_telegram,
        )

        outcome = await confirm_channel_price_offer(
            db_session,
            request_id=request.id,
            actor_employee_id=seed_employee.id,
            actor_telegram_id=seed_employee.telegram_id,
            now=TODAY,
        )
        assert outcome == "published"

        rows = await _client_outbox(db_session)
        assert len(rows) == 1
        assert rows[0].chat_id == seed_group_chat_id
        assert rows[0].dedupe_key == f"request:{request.id}:first-client-price"
        assert "100500" in rows[0].text
        assert request.price_state == RequestPriceState.published

        selection = await db_session.scalar(
            select(RequestPriceSelection).where(
                RequestPriceSelection.request_id == request.id,
                RequestPriceSelection.status == "published",
            )
        )
        assert selection is not None
        assert selection.publication_reason == "channel_price"
        assert selection.published_by == seed_employee.id

        again = await confirm_channel_price_offer(
            db_session, request_id=request.id, now=TODAY
        )
        assert again == "already"
        assert len(await _client_outbox(db_session)) == 1
    finally:
        set_llm_client(None)


@pytest.mark.asyncio
async def test_offer_needs_both_flags(
    db_session: AsyncSession,
    mock_telegram: MockTelegramClient,
    mock_llm: MockLLMClient,
    seed_channel_supplier: Supplier,
    seed_markup_rules,
    seed_employee,
    seed_group_chat_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Проекция есть, флаг оффера выключен → карточки нет."""
    set_llm_client(mock_llm)
    monkeypatch.setenv("CHANNEL_SKU_PRICE_PROJECTION_ENABLED", "true")
    get_settings.cache_clear()
    try:
        await _seed_channel_price(
            db_session, mock_llm, seed_channel_supplier, 100000, post_date=TODAY
        )
        await rebuild_channel_sku_prices(
            db_session, await aggregate_today(db_session), now=TODAY
        )
        request = await _make_request(
            db_session, seed_employee.id, seed_group_chat_id
        )

        assert (
            await offer_channel_price_for_request(
                db_session, request, telegram=mock_telegram, now=TODAY
            )
            is False
        )
        assert mock_telegram.sent == []
        assert (await db_session.scalars(select(RequestPriceSelection))).all() == []
    finally:
        set_llm_client(None)
        get_settings.cache_clear()
