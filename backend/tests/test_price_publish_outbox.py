"""Публикация утверждённого прайса через telegram_outbox (PRICE_PUBLISH_VIA_OUTBOX).

Смысл перехода: сбой отправки перестаёт теряться в момент нажатия — строка
остаётся в очереди и уходит в ретрай/DLQ, а не только в лог.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import PriceListDraft, TelegramOutbox
from app.services import publish_channel_service
from app.services.price_service import approve_price_draft, price_publish_dedupe_key
from tests.conftest import PRICE_PUBLISH_CHAT_ID, MockTelegramClient

MANUAL_CHAT_ID = -1004444444444
ITEMS = [
    {"sku_key": "iphone 15 256", "title": "iPhone 15 256GB Black", "our_price": "100800.00"},
    {"sku_key": "samsung s26 256", "title": "Samsung Galaxy S26 256GB", "our_price": "89990.00"},
]


async def _add_channel(db_session: AsyncSession, mock_telegram: MockTelegramClient) -> None:
    mock_telegram.set_chat(
        MANUAL_CHAT_ID, {"title": "Прайс клиента", "type": "channel"}
    )
    await publish_channel_service.add_publish_channel(
        db_session, telegram=mock_telegram, chat_id=MANUAL_CHAT_ID
    )


async def _publish_rows(db_session: AsyncSession) -> list[TelegramOutbox]:
    return list(
        (
            await db_session.scalars(
                select(TelegramOutbox)
                .where(TelegramOutbox.kind == "price_publish")
                .order_by(TelegramOutbox.chat_id)
            )
        ).all()
    )


async def _draft(db_session: AsyncSession) -> PriceListDraft:
    draft = PriceListDraft(items=ITEMS, status="pending")
    db_session.add(draft)
    await db_session.flush()
    return draft


@pytest.mark.asyncio
async def test_outbox_publish_enqueues_one_row_per_channel(
    db_session: AsyncSession,
    mock_telegram: MockTelegramClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "price_publish_via_outbox", True)
    await _add_channel(db_session, mock_telegram)
    draft = await _draft(db_session)

    outcome = await approve_price_draft(
        db_session, draft_id=draft.id, employee_id=None, telegram=mock_telegram
    )

    assert outcome == "approved_queued"
    rows = await _publish_rows(db_session)
    assert [row.chat_id for row in rows] == sorted([PRICE_PUBLISH_CHAT_ID, MANUAL_CHAT_ID])
    for row in rows:
        assert row.dedupe_key == price_publish_dedupe_key(draft.id, row.chat_id)
        assert row.parse_mode == "HTML"
        assert row.status == "pending"
        assert "<b>iPhone</b>" in row.text and "100 800 ₽" in row.text
        assert "<b>Samsung</b>" in row.text
    assert not [
        chat_id for chat_id, _, _ in mock_telegram.sent if chat_id in {PRICE_PUBLISH_CHAT_ID, MANUAL_CHAT_ID}
    ]

    repeated = await approve_price_draft(
        db_session, draft_id=draft.id, employee_id=None, telegram=mock_telegram
    )
    assert repeated == "already_processed"
    assert len(await _publish_rows(db_session)) == 2


@pytest.mark.asyncio
async def test_disabled_channel_gets_no_message(
    db_session: AsyncSession,
    mock_telegram: MockTelegramClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "price_publish_via_outbox", True)
    await _add_channel(db_session, mock_telegram)
    row = await publish_channel_service.list_publish_channels(db_session)
    manual = next(item for item in row if item.chat_id == MANUAL_CHAT_ID)
    await publish_channel_service.toggle_publish_channel(
        db_session, channel_id=manual.id
    )
    draft = await _draft(db_session)

    await approve_price_draft(
        db_session, draft_id=draft.id, employee_id=None, telegram=mock_telegram
    )

    assert [item.chat_id for item in await _publish_rows(db_session)] == [PRICE_PUBLISH_CHAT_ID]


@pytest.mark.asyncio
async def test_flag_off_keeps_direct_send_without_outbox(
    db_session: AsyncSession,
    mock_telegram: MockTelegramClient,
) -> None:
    await _add_channel(db_session, mock_telegram)
    draft = await _draft(db_session)

    outcome = await approve_price_draft(
        db_session, draft_id=draft.id, employee_id=None, telegram=mock_telegram
    )

    assert outcome == "approved"
    assert await _publish_rows(db_session) == []
    published = [chat_id for chat_id, _, _ in mock_telegram.sent if chat_id in {PRICE_PUBLISH_CHAT_ID, MANUAL_CHAT_ID}]
    assert sorted(published) == sorted([PRICE_PUBLISH_CHAT_ID, MANUAL_CHAT_ID])
