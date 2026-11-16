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
    purpose: str
    status: str
    is_active: bool
    error_text: str | None = None

    model_config = {"from_attributes": True}


class ChannelCreate(BaseModel):
    handle: str = Field(..., description="@username or numeric id")
    purpose: ParserChannelPurpose = ParserChannelPurpose.supplier_price_source


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
        await db.flush()
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
