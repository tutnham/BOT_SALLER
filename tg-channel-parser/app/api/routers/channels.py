"""GET /channels, POST /channels, DELETE /channels/{id}."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import require_api_token
from app.db.models import (
    ParserChannel,
    ParserChannelPurpose,
    ParserChannelStatus,
    ParserStatus,
    ParserTask,
    ParserTaskType,
)
from app.db.session import get_db
from app.mtproto.channel_handle import InvalidChannelHandle, split_channel_handle

router = APIRouter(prefix="/channels", tags=["channels"])


class ChannelOut(BaseModel):
    id: int
    channel_id: int | None
    username: str | None
    title: str | None
    purpose: ParserChannelPurpose
    status: ParserChannelStatus
    is_active: bool
    error_text: str | None = None

    model_config = {"from_attributes": True, "use_enum_values": True}


class ChannelCreate(BaseModel):
    handle: str = Field(..., description="@username or numeric id")
    purpose: ParserChannelPurpose = ParserChannelPurpose.supplier_price_source


class ChannelBackfillIn(BaseModel):
    channel_id: int = Field(..., description="Telegram channel id, including the -100 prefix")
    full_history: bool = Field(default=False, description="True = забрать всю историю канала без лимита")


class ChannelBackfillOut(BaseModel):
    task_id: int
    status: str
    channel_id: int
    error_text: str | None = None


def _channel_matches(
    channel: ParserChannel,
    *,
    username: str | None,
    numeric_id: int | None,
) -> bool:
    if username and channel.username and channel.username.lower() == username.lower():
        return True
    if numeric_id is None:
        return False
    if channel.channel_id == numeric_id:
        return True
    title = (channel.title or "").strip()
    if title.lstrip("-").isdigit() and int(title) == numeric_id:
        return True
    return False


def _duplicate_detail(channel: ParserChannel, *, username: str | None) -> str:
    if channel.status == ParserChannelStatus.pending:
        if username:
            return "pending_channel_username_exists"
        return "pending_channel_id_exists"
    return "channel_already_active"


def _integrity_conflict_detail(exc: IntegrityError) -> str:
    message = str(getattr(exc, "orig", None) or exc).lower()
    if "channel_id" in message:
        return "channel_id_exists"
    return "pending_channel_username_exists"


def _enqueue_backfill_task(
    db: AsyncSession, channel: ParserChannel, *, full_history: bool = False
) -> ParserTask:
    task = ParserTask(
        post_id=None,
        channel_id=channel.id,
        task_type=ParserTaskType.backfill_channel,
        status=ParserStatus.new,
        max_attempts=5,
        history_limit=0 if full_history else None,
    )
    db.add(task)
    return task


def _enqueue_resolve_task(db: AsyncSession, channel: ParserChannel) -> None:
    db.add(
        ParserTask(
            post_id=None,
            channel_id=channel.id,
            task_type=ParserTaskType.resolve_channel,
            status=ParserStatus.new,
            max_attempts=5,
        )
    )


@router.get("", response_model=list[ChannelOut], dependencies=[Depends(require_api_token)])
async def list_channels(
    purpose: ParserChannelPurpose | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> list[ParserChannel]:
    stmt = select(ParserChannel).order_by(ParserChannel.id.asc())
    if purpose is not None:
        stmt = stmt.where(ParserChannel.purpose == purpose)
    result = await db.execute(stmt)
    return list(result.scalars().all())


@router.post(
    "",
    response_model=ChannelOut,
    dependencies=[Depends(require_api_token)],
    status_code=202,
)
async def create_channel(
    payload: ChannelCreate,
    db: AsyncSession = Depends(get_db),
) -> ParserChannel:
    """Register a channel for later MTProto resolution.

    The channel is created with ``status='pending'`` and a background worker
    resolves the numeric ``channel_id`` via ``get_chat``.  The caller must poll
    ``GET /channels`` until ``status`` becomes ``active`` or ``failed``.
    Numeric handles stay in ``title`` until resolve succeeds; ``channel_id``
    is not set while pending.
    """
    try:
        username, numeric_id = split_channel_handle(payload.handle)
    except InvalidChannelHandle as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None

    existing_result = await db.execute(select(ParserChannel))
    existing = next(
        (
            channel
            for channel in existing_result.scalars().all()
            if _channel_matches(channel, username=username, numeric_id=numeric_id)
        ),
        None,
    )
    if existing is not None:
        if existing.status in (ParserChannelStatus.pending, ParserChannelStatus.active) and (
            existing.is_active or existing.status == ParserChannelStatus.pending
        ):
            raise HTTPException(
                status_code=409,
                detail=_duplicate_detail(existing, username=username),
            )
        existing.username = username or existing.username
        existing.title = payload.handle.strip() if numeric_id is not None else existing.title
        existing.purpose = payload.purpose
        existing.status = ParserChannelStatus.pending
        existing.is_active = True
        existing.error_text = None
        _enqueue_resolve_task(db, existing)
        try:
            await db.flush()
        except IntegrityError as exc:
            # Например, uq_parser_channels_pending_username: уже есть pending-строка
            await db.rollback()
            raise HTTPException(
                status_code=409,
                detail=_integrity_conflict_detail(exc),
            ) from None
        await db.commit()
        return existing

    channel = ParserChannel(
        channel_id=None,
        username=username,
        title=payload.handle.strip() if numeric_id is not None else None,
        purpose=payload.purpose,
        status=ParserChannelStatus.pending,
        is_active=True,
    )
    db.add(channel)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail=_integrity_conflict_detail(exc),
        ) from None

    _enqueue_resolve_task(db, channel)
    await db.flush()
    await db.commit()

    return channel


@router.post(
    "/backfill",
    response_model=ChannelBackfillOut,
    dependencies=[Depends(require_api_token)],
    status_code=202,
)
async def enqueue_channel_backfill(
    payload: ChannelBackfillIn,
    db: AsyncSession = Depends(get_db),
) -> ChannelBackfillOut:
    """Поставить ручной забор истории. Забирает её процесс tg-runtime, не API."""
    found = list(
        (
            await db.execute(
                select(ParserChannel).where(ParserChannel.channel_id == payload.channel_id)
            )
        ).scalars().all()
    )
    channel = next((row for row in found if row.channel_id == payload.channel_id), None)
    if channel is None:
        raise HTTPException(status_code=404, detail="channel_not_found")
    if (
        not channel.is_active
        or channel.status != ParserChannelStatus.active
        or channel.channel_id is None
    ):
        raise HTTPException(status_code=409, detail="channel_not_active")

    open_tasks = list(
        (
            await db.execute(
                select(ParserTask).where(
                    ParserTask.channel_id == channel.id,
                    ParserTask.task_type == ParserTaskType.backfill_channel,
                    ParserTask.status.in_((ParserStatus.new, ParserStatus.processing)),
                )
            )
        ).scalars().all()
    )
    task = next(
        (
            row
            for row in open_tasks
            if row.channel_id == channel.id
            and row.task_type == ParserTaskType.backfill_channel
            and row.status in (ParserStatus.new, ParserStatus.processing)
        ),
        None,
    )
    if task is None:
        task = _enqueue_backfill_task(db, channel, full_history=payload.full_history)
        await db.flush()
    elif payload.full_history and task.history_limit != 0:
        task.history_limit = 0
    await db.commit()
    return ChannelBackfillOut(
        task_id=task.id,
        status=task.status.value if isinstance(task.status, ParserStatus) else str(task.status),
        channel_id=channel.channel_id,
        error_text=task.error_text,
    )


@router.get(
    "/backfill/{task_id}",
    response_model=ChannelBackfillOut,
    dependencies=[Depends(require_api_token)],
)
async def get_channel_backfill(
    task_id: int,
    db: AsyncSession = Depends(get_db),
) -> ChannelBackfillOut:
    task = await db.get(ParserTask, task_id)
    if task is None or task.task_type != ParserTaskType.backfill_channel:
        raise HTTPException(status_code=404, detail="task_not_found")
    channel = await db.get(ParserChannel, task.channel_id) if task.channel_id is not None else None
    return ChannelBackfillOut(
        task_id=task.id,
        status=task.status.value if isinstance(task.status, ParserStatus) else str(task.status),
        channel_id=channel.channel_id if channel is not None and channel.channel_id is not None else 0,
        error_text=task.error_text,
    )


@router.delete(
    "/{channel_id}",
    dependencies=[Depends(require_api_token)],
)
async def delete_channel(
    channel_id: int,
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    channel = await db.get(ParserChannel, channel_id)
    if channel is None:
        raise HTTPException(status_code=404, detail="channel_not_found")
    channel.is_active = False
    channel.status = ParserChannelStatus.failed
    channel.error_text = "deleted_by_admin"
    await db.execute(
        update(ParserTask)
        .where(
            ParserTask.channel_id == channel.id,
            ParserTask.task_type == ParserTaskType.resolve_channel,
            ParserTask.status.in_((ParserStatus.new, ParserStatus.processing)),
        )
        .values(status=ParserStatus.failed, error_text="cancelled")
    )
    await db.flush()
    return {"status": "ok"}
