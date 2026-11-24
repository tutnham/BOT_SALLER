"""Parser task processors (resolve_channel, download_media).

Poll loop runs in ``app.mtproto.runtime`` (single Pyrogram client).
Legacy entrypoint delegates to runtime.
"""

from __future__ import annotations

import asyncio
import mimetypes
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from loguru import logger
from pyrogram.errors import PeerIdInvalid
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.db.models import (
    ParserChannel,
    ParserChannelPurpose,
    ParserChannelStatus,
    ParserPost,
    ParserStatus,
    ParserTask,
    ParserTaskType,
)
from app.db.session import get_session_factory
from app.mtproto.channel_backfill import backfill_channel_on_activate
from app.mtproto.channel_handle import mtproto_chat_ref
from app.mtproto.flood_wait import with_flood_wait
from app.mtproto.upsert import get_channel_by_tg_id
from app.storage.local_storage import LocalStorage, guess_ext, media_key
from app.utils.hashing import sha256_file


async def claim_next_task(session: AsyncSession) -> ParserTask | None:
    """Pick one due task: resolve_channel first, then backfill_channel, then download_media."""
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

    backfill_stmt = (
        select(ParserTask)
        .join(ParserChannel, ParserChannel.id == ParserTask.channel_id)
        .where(
            ParserTask.status == ParserStatus.new,
            ParserTask.task_type == ParserTaskType.backfill_channel,
            ParserTask.scheduled_at <= now,
            ParserChannel.status == ParserChannelStatus.active,
        )
        .order_by(ParserTask.scheduled_at.asc())
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    result = await session.execute(backfill_stmt)
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


async def _resolve_channel_via_dialogs(
    client: Any,
    target: int,
) -> tuple[Any | None, str | None]:
    from pyrogram.enums import ChatType

    dialogs = client.get_dialogs()
    try:
        async for dialog in dialogs:
            chat = dialog.chat
            if chat.id != target:
                continue
            if chat.type != ChatType.CHANNEL:
                return None, "peer_not_channel"
            return chat, None
    except Exception as exc:
        return None, f"dialog_discovery_error:{type(exc).__name__}"
    finally:
        aclose = getattr(dialogs, "aclose", None)
        if aclose is not None:
            await aclose()
    return None, "channel_not_in_dialogs"


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

    target = mtproto_chat_ref(channel.channel_id, channel.username, channel.title)
    if target is None:
        task.status = ParserStatus.failed
        task.error_text = "no_handle"
        channel.status = ParserChannelStatus.failed
        channel.error_text = "no_handle"
        return

    try:
        chat = await with_flood_wait(lambda: client.get_chat(target))
    except Exception as exc:
        chat = None
        # Эвристика: положительный ID канала без префикса -100 (формат Bot API)
        if isinstance(target, int) and target > 0 and isinstance(exc, PeerIdInvalid):
            canonical = int(f"-100{target}")
            try:
                chat = await with_flood_wait(lambda: client.get_chat(canonical))
            except Exception:
                chat = None
        if chat is None:
            # Pyrogram 2.x отклоняет oversized -100 ID до MTProto при холодном cache.
            # Dialogs прогревает peer cache и доказывает точный доступ к каналу.
            if (
                isinstance(target, int)
                and target < -100_000_000_000
                and isinstance(exc, ValueError | PeerIdInvalid)
            ):
                chat, dialog_error = await _resolve_channel_via_dialogs(client, target)
                if chat is None:
                    await _fail_or_retry(
                        session,
                        task,
                        None,
                        f"resolve_error:{type(exc).__name__}:{exc};dialog_fallback:{dialog_error}",
                    )
                    if task.status == ParserStatus.failed:
                        channel.status = ParserChannelStatus.failed
                        channel.error_text = task.error_text
                    return
        if chat is None:
            await _fail_or_retry(
                session,
                task,
                None,
                f"resolve_error:{type(exc).__name__}:{exc}",
            )
            if task.status == ParserStatus.failed:
                channel.status = ParserChannelStatus.failed
                channel.error_text = task.error_text
            return

    if chat is None:
        await _fail_or_retry(session, task, None, "chat_not_found")
        if task.status == ParserStatus.failed:
            channel.status = ParserChannelStatus.failed
            channel.error_text = task.error_text
        return

    # Коллизия UNIQUE channel_id: тот же канал уже зарегистрирован другой строкой
    # (например, добавлен и по @username, и по числовому ID).
    existing = await get_channel_by_tg_id(session, int(chat.id))
    if existing is not None and existing.id != channel.id:
        existing.is_active = True
        existing.status = ParserChannelStatus.active
        existing.error_text = None
        existing.username = getattr(chat, "username", None) or existing.username
        existing.title = getattr(chat, "title", None) or existing.title
        channel.status = ParserChannelStatus.failed
        channel.error_text = f"duplicate_of_channel:{existing.id}"
        task.status = ParserStatus.processed
        task.error_text = channel.error_text
        await session.flush()
        logger.warning(
            "Resolve duplicate: channel row {} merged into {} (tg_id={})",
            channel.id,
            existing.id,
            chat.id,
        )
        return

    channel.channel_id = chat.id
    channel.username = getattr(chat, "username", None) or channel.username
    channel.title = getattr(chat, "title", None) or channel.title
    channel.status = ParserChannelStatus.active
    channel.error_text = None
    task.status = ParserStatus.processed
    task.error_text = None
    await session.flush()
    try:
        await backfill_channel_on_activate(client, channel=channel)
    except Exception as exc:
        logger.warning(
            "Backfill after resolve failed channel_id={}: {}",
            channel.channel_id,
            exc,
        )
        # B4: канал уже active, но без истории — планируем повторный backfill
        settings = get_settings()
        session.add(
            ParserTask(
                post_id=None,
                channel_id=channel.id,
                task_type=ParserTaskType.backfill_channel,
                status=ParserStatus.new,
                attempts=0,
                max_attempts=settings.media_max_retries,
                scheduled_at=datetime.now(UTC)
                + timedelta(seconds=settings.media_retry_backoff_base_seconds),
            )
        )
        await session.flush()


async def process_backfill_channel(
    session: AsyncSession,
    task: ParserTask,
    client: Any,
) -> None:
    """Повторный backfill истории канала после сбоя при resolve (B4)."""
    if task.channel_id is None:
        task.status = ParserStatus.failed
        task.error_text = "missing_channel_id"
        return

    channel = await session.get(ParserChannel, task.channel_id)
    if channel is None or channel.status != ParserChannelStatus.active:
        task.status = ParserStatus.processed
        task.error_text = "channel_not_active"
        return

    try:
        await session.refresh(task)
        initial_limit = task.history_limit
        await backfill_channel_on_activate(
            client, channel=channel, limit_override=initial_limit
        )
        await session.refresh(task)
        if task.history_limit == 0 and initial_limit != 0:
            await backfill_channel_on_activate(
                client, channel=channel, limit_override=0
            )
    except Exception as exc:
        await _fail_or_retry(
            session,
            task,
            None,
            f"backfill_error:{type(exc).__name__}:{exc}",
        )
        return
    task.status = ParserStatus.processed
    task.error_text = None


async def skip_supplier_price_media_downloads(session: AsyncSession) -> int:
    """Снять скачивание файлов у прайс-каналов: боту нужен только текст поста."""
    price_posts = (
        select(ParserPost.id)
        .join(ParserChannel, ParserChannel.channel_id == ParserPost.channel_id)
        .where(ParserChannel.purpose == ParserChannelPurpose.supplier_price_source)
    )
    result = await session.execute(
        update(ParserTask)
        .where(
            ParserTask.task_type == ParserTaskType.download_media,
            ParserTask.status.in_((ParserStatus.new, ParserStatus.processing)),
            ParserTask.post_id.in_(price_posts),
        )
        .values(status=ParserStatus.failed, error_text="price_channel_text_only")
    )
    return int(result.rowcount or 0)


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
    channel = await get_channel_by_tg_id(session, post.channel_id)
    if channel is not None and channel.purpose == ParserChannelPurpose.supplier_price_source:
        task.status = ParserStatus.failed
        task.error_text = "price_channel_text_only"
        return
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
        elif task.task_type == ParserTaskType.backfill_channel:
            await process_backfill_channel(session, task, client)
        elif task.task_type == ParserTaskType.download_media:
            await process_download_media(session, task, client)
        else:
            task.status = ParserStatus.failed
            task.error_text = f"unsupported_task_type:{task.task_type}"
        await session.commit()


async def _recover_crashed_task(task_id: int, error: str) -> None:
    """Пометить задачу, уронившую обработчик, через retry/failed в отдельной сессии."""
    factory = get_session_factory()
    try:
        async with factory() as session:
            task = await session.get(ParserTask, task_id)
            if task is None or task.status != ParserStatus.processing:
                return
            post = (
                await session.get(ParserPost, task.post_id)
                if task.post_id is not None
                else None
            )
            await _fail_or_retry(session, task, post, error)
            await session.commit()
    except Exception:
        logger.exception("Failed to recover crashed task_id={}", task_id)


async def run_task_poll_loop(client: Any) -> None:
    """Claim and process parser_tasks using the shared MTProto client."""
    settings = get_settings()
    while True:
        factory = get_session_factory()
        async with factory() as session:
            reclaimed = await reclaim_stale_processing(session)
            if reclaimed:
                logger.warning("Reclaimed {} stale processing tasks", reclaimed)
            skipped = await skip_supplier_price_media_downloads(session)
            if skipped:
                logger.info("Skipped {} price-channel media downloads", skipped)
            await session.commit()
        task_id = await _claim_next_task_id()
        if task_id is None:
            await asyncio.sleep(settings.task_poll_interval_seconds)
            continue
        try:
            await _process_claimed_task(task_id, client)
        except Exception as exc:
            # Ошибка одного обработчика не должна убивать весь воркер:
            # иначе каналы навсегда остаются pending при живом heartbeat.
            logger.exception("Task {} crashed the processor", task_id)
            await _recover_crashed_task(
                task_id, f"processor_crash:{type(exc).__name__}:{exc}"
            )


def main() -> None:
    from app.mtproto.runtime import main as runtime_main

    runtime_main()


if __name__ == "__main__":
    main()
