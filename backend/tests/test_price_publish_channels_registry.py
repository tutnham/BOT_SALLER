"""Реестр price_publish_channels: env-fallback, проверка доступа, тумблеры."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AdminAuditLog, PricePublishChannel
from app.services import publish_channel_service
from tests.conftest import MockTelegramClient

ENV_CHAT_ID = -1002222222222
MANUAL_CHAT_ID = -1003333333333


async def _rows(db_session: AsyncSession) -> list[PricePublishChannel]:
    return list((await db_session.scalars(select(PricePublishChannel))).all())


@pytest.mark.asyncio
async def test_seed_from_env_is_idempotent(db_session: AsyncSession) -> None:
    assert await publish_channel_service.seed_publish_channels_from_env(db_session) == 1
    assert await publish_channel_service.seed_publish_channels_from_env(db_session) == 0

    rows = await _rows(db_session)
    assert [row.chat_id for row in rows] == [ENV_CHAT_ID]
    assert rows[0].source == "env"
    assert rows[0].active is True
    assert await publish_channel_service.resolve_publish_targets(db_session) == [ENV_CHAT_ID]


@pytest.mark.asyncio
async def test_add_rejects_chat_bot_cannot_see(
    db_session: AsyncSession,
    mock_telegram: MockTelegramClient,
    seed_owner,
) -> None:
    with pytest.raises(publish_channel_service.PublishChannelError):
        await publish_channel_service.add_publish_channel(
            db_session,
            telegram=mock_telegram,
            chat_id=MANUAL_CHAT_ID,
            created_by_telegram_id=seed_owner.telegram_id,
        )
    assert await _rows(db_session) == []


@pytest.mark.asyncio
async def test_add_rejects_duplicate(
    db_session: AsyncSession,
    mock_telegram: MockTelegramClient,
    seed_owner,
) -> None:
    mock_telegram.set_chat(
        MANUAL_CHAT_ID, {"title": "Прайс клиента", "type": "channel"}
    )
    await publish_channel_service.add_publish_channel(
        db_session,
        telegram=mock_telegram,
        chat_id=MANUAL_CHAT_ID,
        created_by_telegram_id=seed_owner.telegram_id,
    )
    with pytest.raises(publish_channel_service.PublishChannelError):
        await publish_channel_service.add_publish_channel(
            db_session, telegram=mock_telegram, chat_id=MANUAL_CHAT_ID
        )
    assert len(await _rows(db_session)) == 1


@pytest.mark.asyncio
async def test_toggle_and_delete_change_targets(
    db_session: AsyncSession,
    mock_telegram: MockTelegramClient,
    seed_owner,
) -> None:
    mock_telegram.set_chat(
        MANUAL_CHAT_ID, {"title": "Прайс клиента", "type": "channel"}
    )
    row = await publish_channel_service.add_publish_channel(
        db_session,
        telegram=mock_telegram,
        chat_id=MANUAL_CHAT_ID,
        created_by_telegram_id=seed_owner.telegram_id,
    )
    assert row.title == "Прайс клиента"
    assert set(await publish_channel_service.resolve_publish_targets(db_session)) == {
        ENV_CHAT_ID,
        MANUAL_CHAT_ID,
    }

    off = await publish_channel_service.toggle_publish_channel(
        db_session, channel_id=row.id, actor_telegram_id=seed_owner.telegram_id
    )
    assert off is not None and off.active is False
    assert await publish_channel_service.resolve_publish_targets(db_session) == [ENV_CHAT_ID]

    label = await publish_channel_service.delete_publish_channel(
        db_session, channel_id=row.id, actor_telegram_id=seed_owner.telegram_id
    )
    assert label == "Прайс клиента"
    assert [r.chat_id for r in await _rows(db_session)] == [ENV_CHAT_ID]

    actions = [
        action
        for action in (
            await db_session.scalars(
                select(AdminAuditLog.action).where(
                    AdminAuditLog.entity_type == "price_publish_channel"
                )
            )
        ).all()
    ]
    assert actions == ["publish_channel_add", "publish_channel_toggle", "publish_channel_delete"]
