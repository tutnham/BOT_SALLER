"""Idempotent upsert of posts + task creation (doc §6.3, §7.4)."""

from __future__ import annotations

from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import (
    ParserChannel,
    ParserChannelPurpose,
    ParserChannelStatus,
    ParserContentType,
    ParserMediaFile,
    ParserPost,
    ParserStatus,
    ParserStorageBackend,
    ParserTask,
    ParserTaskType,
)
from app.extraction.post_mapper import (
    MEDIA_CONTENT_TYPES,
    extract_media_descriptors,
    map_message_to_post_fields,
)


async def get_channel_by_tg_id(
    session: AsyncSession, channel_id: int
) -> ParserChannel | None:
    result = await session.execute(
        select(ParserChannel).where(ParserChannel.channel_id == channel_id)
    )
    return result.scalar_one_or_none()


async def upsert_channel(
    session: AsyncSession,
    *,
    channel_id: int,
    username: str | None,
    title: str | None,
    purpose: ParserChannelPurpose = ParserChannelPurpose.monitoring,
    status: ParserChannelStatus = ParserChannelStatus.active,
    error_text: str | None = None,
) -> ParserChannel:
    stmt = (
        insert(ParserChannel)
        .values(
            channel_id=channel_id,
            username=username,
            title=title,
            purpose=purpose,
            status=status,
            error_text=error_text,
        )
        .on_conflict_do_update(
            index_elements=[ParserChannel.channel_id],
            set_={
                "username": username,
                "title": title,
                "purpose": purpose,
                "status": status,
                "error_text": error_text,
            },
        )
        .returning(ParserChannel)
    )
    result = await session.execute(stmt)
    channel = result.scalar_one()
    await session.flush()
    return channel


async def upsert_post(session: AsyncSession, message: Any) -> ParserPost | None:
    """
    Insert post ON CONFLICT DO NOTHING.
    On real insert: create download_media / ai_process tasks per purpose (§7.4).
    Returns post row if newly inserted, else None.
    """
    fields = map_message_to_post_fields(message)
    channel = await get_channel_by_tg_id(session, fields["channel_id"])
    if channel is None:
        logger.warning(
            "Skipping post: channel_id={} not registered",
            fields["channel_id"],
        )
        return None

    stmt = (
        insert(ParserPost)
        .values(
            channel_id=fields["channel_id"],
            message_id=fields["message_id"],
            grouped_id=fields["grouped_id"],
            post_date=fields["post_date"],
            content_type=fields["content_type"],
            raw_text=fields["raw_text"],
            message_link=fields["message_link"],
            raw_metadata=fields["raw_metadata"],
            status=ParserStatus.new,
        )
        .on_conflict_do_nothing(index_elements=["channel_id", "message_id"])
        .returning(ParserPost)
    )
    result = await session.execute(stmt)
    post = result.scalar_one_or_none()
    if post is None:
        # conflict — already exists
        return None

    # Media descriptors
    backend = (
        ParserStorageBackend.local
        if get_settings().media_storage_backend == "local"
        else ParserStorageBackend.s3
    )
    for desc in extract_media_descriptors(message):
        media_stmt = (
            insert(ParserMediaFile)
            .values(
                post_id=post.id,
                media_type=desc["media_type"],
                file_unique_id=desc["file_unique_id"],
                file_id=desc.get("file_id"),
                storage_backend=backend,
                file_size=desc.get("file_size"),
                mime_type=desc.get("mime_type"),
                download_status=ParserStatus.new,
            )
            .on_conflict_do_nothing(index_elements=["post_id", "file_unique_id"])
        )
        await session.execute(media_stmt)

    # Tasks per purpose (§7.4)
    content_type: ParserContentType = fields["content_type"]
    if content_type in MEDIA_CONTENT_TYPES or extract_media_descriptors(message):
        await session.execute(
            insert(ParserTask).values(
                post_id=post.id,
                task_type=ParserTaskType.download_media,
                status=ParserStatus.new,
                max_attempts=get_settings().media_max_retries,
            )
        )

    if channel.purpose == ParserChannelPurpose.monitoring:
        await session.execute(
            insert(ParserTask).values(
                post_id=post.id,
                task_type=ParserTaskType.ai_process,
                status=ParserStatus.new,
            )
        )
    # supplier_price_source: no ai_process (zakupki LLM parses prices)

    if content_type == ParserContentType.other:
        post.error_text = "unsupported_media_type"

    await session.flush()
    return post
