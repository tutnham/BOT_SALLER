"""GET /channels, POST /channels, DELETE /channels/{id}."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import require_api_token
from app.db.models import ParserChannel, ParserChannelPurpose, ParserChannelStatus, ParserStatus, ParserTask, ParserTaskType
from app.db.session import get_db

router = APIRouter(prefix="/channels", tags=["channels"])


class ChannelOut(BaseModel):
    id: int
    channel_id: int | None
    username: str | None
    title: str | None
    purpose: str
    status: str
    is_active: bool

    model_config = {"from_attributes": True}


class ChannelCreate(BaseModel):
    handle: str = Field(..., description="@username or numeric id")
    purpose: ParserChannelPurpose = ParserChannelPurpose.supplier_price_source


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
    """
    handle = payload.handle.strip()
    normalized_username = handle.lstrip("@")

    channel = ParserChannel(
        channel_id=None,
        username=normalized_username if not normalized_username.lstrip("-").isdigit() else None,
        title=handle,
        purpose=payload.purpose,
        status=ParserChannelStatus.pending,
        is_active=True,
    )
    db.add(channel)
    await db.flush()

    task = ParserTask(
        post_id=None,
        channel_id=channel.id,
        task_type=ParserTaskType.resolve_channel,
        status=ParserStatus.new,
    )
    db.add(task)
    await db.flush()

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
    await db.flush()
    return {"status": "ok"}
