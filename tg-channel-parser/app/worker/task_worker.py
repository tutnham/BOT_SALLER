"""Poll parser_tasks and process download_media (MVP — no ai_process).

Usage:
  python -m app.worker.task_worker
"""

from __future__ import annotations

import asyncio
import mimetypes
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from loguru import logger
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.db.models import (
    ParserChannel,
    ParserChannelStatus,
    ParserPost,
    ParserStatus,
    ParserTask,
    ParserTaskType,
)
from app.db.session import dispose_engine, get_session_factory
from app.logging_setup import setup_logging
from app.mtproto.client_factory import create_client
from app.mtproto.flood_wait import with_flood_wait
from app.storage.local_storage import LocalStorage, guess_ext, media_key
from app.utils.hashing import sha256_file


async def claim_next_task(session: AsyncSession) -> ParserTask | None:
    """Pick one due task: resolve_channel first, then download_media."""
    now = datetime.now(UTC)

    resolve_stmt = (
        select(ParserTask)
        .join(ParserChannel, ParserChannel.id == ParserTask.channel_id)
        .where(
            ParserTask.status == ParserStatus.new,
            ParserTask.task_type == ParserTaskType.resolve_channel,
            ParserTask.scheduled_at <= now,
            ParserChannel.status == ParserChannelStatus.pending,
        )
        .order_by(ParserTask.scheduled_at.asc())
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    result = await session.execute(resolve_stmt)
    task = result.scalar_one_or_none()
    if task is not None:
        task.status = ParserStatus.processing
        task.last_attempt_at = now
        task.attempts = (task.attempts or 0) + 1
        await session.flush()
        return task

    download_stmt = (
        select(ParserTask)
        .join(ParserPost, ParserPost.id == ParserTask.post_id)
        .where(
            ParserTask.status == ParserStatus.new,
            ParserTask.task_type == ParserTaskType.download_media,
            ParserTask.scheduled_at <= now,
            ParserPost.excluded.is_(False),
        )
        .order_by(ParserTask.scheduled_at.asc())
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    result = await session.execute(download_stmt)
    task = result.scalar_one_or_none()
    if task is None:
        return None
    task.status = ParserStatus.processing
    task.last_attempt_at = now
    task.attempts = (task.attempts or 0) + 1
    await session.flush()
    return task


async def reclaim_stale_processing(session: AsyncSession) -> int:
    """Return stuck processing tasks to new so a crashed worker cannot hang them."""
    settings = get_settings()
    cutoff = datetime.now(UTC) - timedelta(
        minutes=settings.task_stale_processing_minutes
    )
    result = await session.execute(
        update(ParserTask)
        .where(
            ParserTask.status == ParserStatus.processing,
            ParserTask.last_attempt_at.is_not(None),
            ParserTask.last_attempt_at < cutoff,
        )
        .values(
            status=ParserStatus.new,
            error_text="reclaimed_stale_processing",
        )
    )
    return int(result.rowcount or 0)


async def _fail_or_retry(
    session: AsyncSession,
    task: ParserTask,
    post: ParserPost | None,
    error: str,
) -> None:
    settings = get_settings()
    task.error_text = error
    if task.attempts >= task.max_attempts:
        task.status = ParserStatus.failed
        if post is not None:
            post.status = ParserStatus.failed
            post.error_text = error
        logger.error("Task {} failed permanently: {}", task.id, error)
    else:
        delay = settings.media_retry_backoff_base_seconds * (2 ** (task.attempts - 1))
        task.status = ParserStatus.new
        task.scheduled_at = datetime.now(UTC) + timedelta(seconds=delay)
        logger.warning(
            "Task {} retry in {}s attempt={}: {}",
            task.id,
            delay,
            task.attempts,
            error,
        )


async def process_resolve_channel(
    session: AsyncSession,
    task: ParserTask,
    client: Any,
) -> None:
    """Resolve pending channel handle to numeric channel_id via MTProto."""
    if task.channel_id is None:
        task.status = ParserStatus.failed
        task.error_text = "missing_channel_id"
        return

    channel = await session.get(ParserChannel, task.channel_id)
    if channel is None or channel.status != ParserChannelStatus.pending:
        task.status = ParserStatus.processed
        task.error_text = "channel_not_pending"
        return

    handle = channel.username or channel.title or channel.channel_id
    if handle is None:
        task.status = ParserStatus.failed
        task.error_text = "no_handle"
        channel.status = ParserChannelStatus.failed
        channel.error_text = "no_handle"
        return

    try:
        chat = await with_flood_wait(lambda: client.get_chat(handle))
    except Exception as exc:
        task.status = ParserStatus.failed
        task.error_text = f"resolve_error:{type(exc).__name__}:{exc}"
        channel.status = ParserChannelStatus.failed
        channel.error_text = task.error_text
        return

    if chat is None:
        task.status = ParserStatus.failed
        task.error_text = "chat_not_found"
        channel.status = ParserChannelStatus.failed
        channel.error_text = "chat_not_found"
        return

    channel.channel_id = chat.id
    channel.username = getattr(chat, "username", None) or channel.username
    channel.title = getattr(chat, "title", None) or channel.title
    channel.status = ParserChannelStatus.active
    channel.error_text = None
    task.status = ParserStatus.processed
    task.error_text = None


async def process_download_media(
    session: AsyncSession,
    task: ParserTask,
    client: Any,
) -> None:
    result = await session.execute(
        select(ParserPost)
        .options(selectinload(ParserPost.media_files))
        .where(ParserPost.id == task.post_id)
    )
    post = result.scalar_one()
    if post.excluded:
        task.status = ParserStatus.processed
        task.error_text = "excluded"
        return

    settings = get_settings()
    storage = LocalStorage(settings.media_local_path)
    tmp_dir = Path(settings.media_tmp_path)
    tmp_dir.mkdir(parents=True, exist_ok=True)

    media_rows = list(post.media_files)
    if not media_rows:
        task.status = ParserStatus.processed
        post.status = ParserStatus.downloaded
        return

    try:
        message = await with_flood_wait(
            lambda: client.get_messages(post.channel_id, post.message_id)
        )
        if message is None:
            await _fail_or_retry(session, task, post, "message_not_found")
            return

        for media in media_rows:
            if media.download_status == ParserStatus.downloaded and media.storage_path:
                continue
            if media.file_size is not None and media.file_size > settings.max_media_bytes:
                media.download_status = ParserStatus.failed
                media.error_text = "file_too_large"
                continue
            dest = str(tmp_dir / f"{media.file_unique_id}.tmp")

            def _download(path: str = dest) -> Any:
                return client.download_media(message, file_name=path)

            tmp_path = await with_flood_wait(_download)
            if not tmp_path:
                media.download_status = ParserStatus.failed
                media.error_text = "download_returned_empty"
                continue

            tmp_path_str = str(tmp_path)
            size = await asyncio.to_thread(os.path.getsize, tmp_path_str)
            if size > settings.max_media_bytes:
                media.download_status = ParserStatus.failed
                media.error_text = "file_too_large"
                try:
                    os.remove(tmp_path_str)
                except OSError:
                    pass
                continue
            digest = await asyncio.to_thread(sha256_file, tmp_path_str)
            mime = media.mime_type or mimetypes.guess_type(tmp_path_str)[0]
            ext = guess_ext(tmp_path_str, mime)
            key = media_key(post.channel_id, post.message_id, media.file_unique_id, ext)
            storage_path = await storage.save(tmp_path_str, key)
            media.storage_path = storage_path
            media.sha256_hash = digest
            media.mime_type = mime
            media.file_size = size
            media.download_status = ParserStatus.downloaded
            media.downloaded_at = datetime.now(UTC)
            media.error_text = None
            try:
                os.remove(tmp_path_str)
            except OSError:
                pass

        failed = any(m.download_status == ParserStatus.failed for m in media_rows)
        if failed:
            await _fail_or_retry(session, task, post, "one_or_more_media_failed")
        else:
            task.status = ParserStatus.processed
            post.status = ParserStatus.downloaded
            task.error_text = None
    except Exception as exc:
        await _fail_or_retry(session, task, post, f"{type(exc).__name__}:{exc}")


async def _claim_next_task_id() -> int | None:
    factory = get_session_factory()
    async with factory() as session:
        task = await claim_next_task(session)
        if task is None:
            return None
        task_id = task.id
        await session.commit()
        return task_id


async def _process_claimed_task(task_id: int, client: Any) -> None:
    factory = get_session_factory()
    async with factory() as session:
        task = await session.get(ParserTask, task_id)
        if task is None or task.status != ParserStatus.processing:
            return
        if task.task_type == ParserTaskType.resolve_channel:
            await process_resolve_channel(session, task, client)
        elif task.task_type == ParserTaskType.download_media:
            await process_download_media(session, task, client)
        else:
            task.status = ParserStatus.failed
            task.error_text = f"unsupported_task_type:{task.task_type}"
        await session.commit()


async def worker_loop() -> None:
    settings = get_settings()
    setup_logging(settings.log_level, settings.secret_values())
    settings.require_mtproto()

    client = create_client("worker", settings=settings)
    await client.start()
    logger.info("tg-worker started (download_media only)")
    try:
        while True:
            factory = get_session_factory()
            async with factory() as session:
                reclaimed = await reclaim_stale_processing(session)
                if reclaimed:
                    logger.warning("Reclaimed {} stale processing tasks", reclaimed)
                await session.commit()
            task_id = await _claim_next_task_id()
            if task_id is None:
                await asyncio.sleep(settings.task_poll_interval_seconds)
                continue
            await _process_claimed_task(task_id, client)
    finally:
        await client.stop()
        await dispose_engine()


def main() -> None:
    try:
        asyncio.run(worker_loop())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
