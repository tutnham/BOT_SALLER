"""Реестр каналов публикации утверждённого прайса.

Раньше цель публикации жила только в ``PRICE_PUBLISH_CHAT_IDS``, и добавить
канал можно было только через перезапуск контейнера. Здесь список становится
данными: включение/выключение без деплоя, env остаётся fallback на один релиз
(``seed_publish_channels_from_env`` переносит его строки при первом обращении).
"""

from __future__ import annotations

from datetime import UTC, datetime

from loguru import logger
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import PricePublishChannel
from app.telegram.client import TelegramClientProtocol


class PublishChannelError(Exception):
    """Канал нельзя добавить: бот его не видит или дубликат."""


async def list_publish_channels(
    session: AsyncSession, *, active_only: bool = False
) -> list[PricePublishChannel]:
    stmt = select(PricePublishChannel).order_by(
        PricePublishChannel.active.desc(), PricePublishChannel.chat_id
    )
    if active_only:
        stmt = stmt.where(PricePublishChannel.active.is_(True))
    return list((await session.scalars(stmt)).all())


async def seed_publish_channels_from_env(session: AsyncSession) -> int:
    """Перенести PRICE_PUBLISH_CHAT_IDS в таблицу; повторные вызовы — no-op."""
    env_ids = get_settings().price_publish_chat_id_list
    written = 0
    for chat_id in env_ids:
        result = await session.execute(
            pg_insert(PricePublishChannel)
            .values(chat_id=int(chat_id), source="env", title=None, active=True)
            .on_conflict_do_nothing(index_elements=["chat_id"])
        )
        written += result.rowcount or 0
    if written:
        await session.flush()
        logger.info("price_publish_channels seeded from env: {}", written)
    return written


async def resolve_publish_targets(session: AsyncSession) -> list[int]:
    """Активные цели публикации: реестр, а пока он пуст — env."""
    await seed_publish_channels_from_env(session)
    rows = await list_publish_channels(session, active_only=True)
    return [int(row.chat_id) for row in rows]


async def add_publish_channel(
    session: AsyncSession,
    *,
    telegram: TelegramClientProtocol,
    chat_id: int,
    title: str | None = None,
    created_by_telegram_id: int | None = None,
) -> PricePublishChannel:
    """Проверить доступ бота до записи: битый chat_id не должен попасть в реестр."""
    existing = await session.scalar(
        select(PricePublishChannel.id).where(PricePublishChannel.chat_id == int(chat_id))
    )
    if existing is not None:
        raise PublishChannelError("Этот канал уже в списке.")
    info = await telegram.get_chat(int(chat_id))
    if info is None:
        raise PublishChannelError(
            f"Бот не видит чат {chat_id}. Добавьте бота администратором канала."
        )
    if info.get("type") not in {"channel", "supergroup"}:
        raise PublishChannelError("Нужен канал или супергруппа, не личный чат.")
    row = PricePublishChannel(
        chat_id=int(chat_id),
        title=title or str(info.get("title") or info.get("username") or chat_id),
        source="manual",
        active=True,
        created_by_telegram_id=created_by_telegram_id,
    )
    session.add(row)
    await session.flush()

    from app.services.audit_service import record_admin_mutation

    await record_admin_mutation(
        session,
        action="publish_channel_add",
        entity_type="price_publish_channel",
        entity_id=str(row.chat_id),
        actor_telegram_id=created_by_telegram_id,
        new_state={"title": row.title},
    )
    return row


async def toggle_publish_channel(
    session: AsyncSession,
    *,
    channel_id: int,
    actor_telegram_id: int | None = None,
) -> PricePublishChannel | None:
    row = await session.get(PricePublishChannel, channel_id)
    if row is None:
        return None
    row.active = not row.active
    row.updated_at = datetime.now(UTC)
    await session.flush()

    from app.services.audit_service import record_admin_mutation

    await record_admin_mutation(
        session,
        action="publish_channel_toggle",
        entity_type="price_publish_channel",
        entity_id=str(row.chat_id),
        actor_telegram_id=actor_telegram_id,
        new_state={"active": row.active},
    )
    return row


async def delete_publish_channel(
    session: AsyncSession,
    *,
    channel_id: int,
    actor_telegram_id: int | None = None,
) -> str | None:
    """Удалить цель; возвращает её подпись (None, если строки уже нет)."""
    row = await session.get(PricePublishChannel, channel_id)
    if row is None:
        return None
    chat_id = int(row.chat_id)
    label = row.title or str(chat_id)
    await session.delete(row)
    await session.flush()

    from app.services.audit_service import record_admin_mutation

    await record_admin_mutation(
        session,
        action="publish_channel_delete",
        entity_type="price_publish_channel",
        entity_id=str(chat_id),
        actor_telegram_id=actor_telegram_id,
        previous_state={"chat_id": chat_id},
    )
    return label
