"""GET /posts — contract compatible with zakupki parser_client.ParserPost."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import require_api_token
from app.api.pagination import paginate_rows
from app.db.models import ParserContentType, ParserPost
from app.db.session import get_db

router = APIRouter(prefix="/posts", tags=["posts"])

DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 500


class PostOut(BaseModel):
    """Fields consumed by backend/app/services/parser_client.py ParserPost."""

    id: int
    channel_id: int
    message_id: int
    post_date: datetime
    content_type: str
    raw_text: str | None
    message_link: str | None

    model_config = {"from_attributes": True}


class PostsPageOut(BaseModel):
    items: list[PostOut]
    next_cursor: str | None = None


@router.get("", response_model=PostsPageOut, dependencies=[Depends(require_api_token)])
async def list_posts(
    channel_id: int = Query(...),
    content_type: ParserContentType = Query(default=ParserContentType.text),
    from_: datetime | None = Query(default=None, alias="from"),
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    cursor: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> PostsPageOut:
    stmt = (
        select(ParserPost)
        .where(
            ParserPost.channel_id == channel_id,
            ParserPost.content_type == content_type,
            ParserPost.excluded.is_(False),
        )
        .order_by(ParserPost.post_date.asc(), ParserPost.id.asc())
    )
    if from_ is not None:
        stmt = stmt.where(ParserPost.post_date >= from_)
    result = await db.execute(stmt)
    rows = list(result.scalars().all())
    page_rows, next_cursor = paginate_rows(
        rows,
        limit=limit,
        cursor=cursor,
        get_post_date=lambda row: row.post_date,
        get_row_id=lambda row: row.id,
    )
    return PostsPageOut(items=page_rows, next_cursor=next_cursor)


@router.get(
    "/{post_id}",
    response_model=PostOut,
    dependencies=[Depends(require_api_token)],
)
async def get_post(
    post_id: int,
    db: AsyncSession = Depends(get_db),
) -> ParserPost:
    result = await db.execute(select(ParserPost).where(ParserPost.id == post_id))
    post = result.scalar_one_or_none()
    if post is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found")
    return post
