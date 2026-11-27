"""Цена из прайс-канала не уходит клиенту без подтверждения кнопкой."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    RequestPriceSelection,
    RequestStatus,
    Supplier,
)
from app.llm.client import set_llm_client
from app.services.channel_sku_price_service import (
    confirm_channel_price_offer,
    dismiss_channel_price_offer,
    offer_channel_price_for_request,
    rebuild_channel_sku_prices,
)
from app.services.price_service import aggregate_from_raw_price_ids, aggregate_today
from tests.conftest import MockLLMClient, MockTelegramClient
from tests.test_ask_channel_price_offer_confirm import (
    _client_outbox,
    _make_request,
    _seed_offer,
)
from tests.test_channel_sku_price_projection import TODAY, _seed_channel_price


@pytest.mark.asyncio
async def test_dismiss_publishes_nothing(
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
        assert await _client_outbox(db_session) == []

        assert await dismiss_channel_price_offer(db_session, request_id=request.id) is True
        assert await confirm_channel_price_offer(db_session, request_id=request.id) == "no_offer"

        assert await _client_outbox(db_session) == []
        offer = await db_session.scalar(select(RequestPriceSelection))
        assert offer is not None
        assert offer.status == "invalidated"
        assert request.price_state is None
    finally:
        set_llm_client(None)


@pytest.mark.asyncio
async def test_repriced_channel_makes_offer_stale_not_published(
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
    """Канал исправил цену после карточки: старое подтверждение не публикуется."""
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

        later = TODAY + timedelta(hours=3)
        raised_raw = await _seed_channel_price(
            db_session, mock_llm, seed_channel_supplier, 104000, post_date=later
        )
        await rebuild_channel_sku_prices(
            db_session, await aggregate_from_raw_price_ids(db_session, [raised_raw]), now=later
        )

        assert await confirm_channel_price_offer(
            db_session, request_id=request.id, now=later
        ) == "stale"
        assert await _client_outbox(db_session) == []
        assert request.price_state is None

        # Новый снимок получает собственную карточку.
        assert (
            await offer_channel_price_for_request(
                db_session, request, telegram=mock_telegram, now=later
            )
            is True
        )
        offer = await db_session.scalar(
            select(RequestPriceSelection).where(
                RequestPriceSelection.status == "proposed"
            )
        )
        assert offer is not None
        assert offer.client_unit_price == Decimal("104500.00")
    finally:
        set_llm_client(None)


@pytest.mark.asyncio
async def test_closed_request_does_not_publish(
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
        request.status = RequestStatus.closed
        await db_session.flush()

        assert await confirm_channel_price_offer(db_session, request_id=request.id) == "closed"
        assert await _client_outbox(db_session) == []
    finally:
        set_llm_client(None)


@pytest.mark.asyncio
async def test_expired_price_gets_no_offer(
    db_session: AsyncSession,
    mock_telegram: MockTelegramClient,
    mock_llm: MockLLMClient,
    seed_channel_supplier: Supplier,
    seed_markup_rules,
    seed_employee,
    seed_group_chat_id: int,
    channel_price_flags_on: None,
) -> None:
    set_llm_client(mock_llm)
    try:
        await _seed_channel_price(
            db_session, mock_llm, seed_channel_supplier, 100000, post_date=TODAY
        )
        await rebuild_channel_sku_prices(
            db_session, await aggregate_today(db_session), now=TODAY
        )
        request = await _make_request(db_session, seed_employee.id, seed_group_chat_id)

        # Оффер есть, пока цена свежая.
        assert (
            await offer_channel_price_for_request(
                db_session, request, telegram=mock_telegram, now=TODAY
            )
            is True
        )
        assert await _client_outbox(db_session) == []

        # Тот же снимок после срока годности — новой карточки и публикации нет.
        long_after = TODAY + timedelta(days=3)
        assert (
            await offer_channel_price_for_request(
                db_session, request, telegram=mock_telegram, now=long_after
            )
            is False
        )
        assert await confirm_channel_price_offer(
            db_session, request_id=request.id, now=long_after
        ) == "stale"
        assert await _client_outbox(db_session) == []
    finally:
        set_llm_client(None)
