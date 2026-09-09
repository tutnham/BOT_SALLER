"""Deferred price recheck workflow (TECH DOC §9.4)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MessageKind, MessageOut, Request, RequestStatus
from app.services.deal_service import RequestNotFoundError, RequestNotOpenError
from app.services.quote_service import select_best_quote
from app.services.routing_service import resolve_target_chat
from app.telegram.client import TelegramClientProtocol, TelegramSendError
from app.templates.messages_ru import render_template

RECHECK_HOURS_MIN = 2
RECHECK_HOURS_DEFAULT = 3
RECHECK_HOURS_MAX = 5


class InvalidRecheckHoursError(Exception):
    """Hours outside allowed 2..5 window."""


async def schedule_recheck(
    session: AsyncSession,
    *,
    request_id: int,
    hours: int = RECHECK_HOURS_DEFAULT,
) -> Request:
    """
    Mark request for future recheck.

    Raises:
        RequestNotFoundError, RequestNotOpenError, InvalidRecheckHoursError
    """
    if hours < RECHECK_HOURS_MIN or hours > RECHECK_HOURS_MAX:
        raise InvalidRecheckHoursError(hours)

    request = await session.get(Request, request_id)
    if request is None:
        raise RequestNotFoundError(request_id)

    if request.status in (RequestStatus.closed, RequestStatus.cancelled):
        raise RequestNotOpenError(request_id)

    request.status = RequestStatus.needs_recheck
    request.recheck_at = datetime.now(UTC) + timedelta(hours=hours)
    await session.flush()
    return request


async def send_due_rechecks(
    session: AsyncSession,
    telegram: TelegramClientProtocol,
) -> dict[str, Any]:
    """
    Send recheck messages for due requests. Safe to re-run.

    Clears ``recheck_at`` after send (or skip) so cron does not resend.
    Per-request failures do not abort the batch.
    """
    now = datetime.now(UTC)
    result = await session.execute(
        select(Request)
        .where(
            Request.status == RequestStatus.needs_recheck,
            Request.recheck_at.is_not(None),
            Request.recheck_at <= now,
        )
        .order_by(Request.recheck_at.asc())
        .limit(50)
    )
    due_requests = list(result.scalars().all())

    processed = 0
    skipped = 0
    failed = 0

    for request in due_requests:
        try:
            best = await select_best_quote(
                session, request.id, prefer_bargain=True
            )
            supplier = best.supplier if best is not None else None
            if supplier is None or not supplier.active:
                logger.warning(
                    "Recheck skip request_id={}: no eligible supplier",
                    request.id,
                )
                request.recheck_at = None
                try:
                    await telegram.send_message(
                        request.group_chat_id,
                        render_template(
                            "recheck_skipped",
                            request_id=request.id,
                        ),
                    )
                except TelegramSendError as exc:
                    logger.warning(
                        "Failed to notify group about recheck skip "
                        "request_id={}: {}",
                        request.id,
                        exc,
                    )
                skipped += 1
            else:
                chat_id = await resolve_target_chat(session, supplier.id)
                if chat_id is None:
                    logger.warning(
                        "Recheck skip request_id={}: no chat for supplier_id={}",
                        request.id,
                        supplier.id,
                    )
                    request.recheck_at = None
                    skipped += 1
                    continue
                text = render_template("recheck", request_id=request.id)
                message_id = await telegram.send_message(chat_id, text)
                session.add(
                    MessageOut(
                        request_id=request.id,
                        supplier_id=supplier.id,
                        tg_message_id=message_id,
                        chat_id=chat_id,
                        text=text,
                        kind=MessageKind.recheck,
                    )
                )
                # Keep status needs_recheck until human closes/cancels/deals.
                request.recheck_at = None
                processed += 1
        except Exception as exc:
            logger.warning(
                "Recheck failed request_id={}: {}",
                request.id,
                exc,
            )
            # Avoid tight retry loop on persistent failures.
            request.recheck_at = now + timedelta(hours=1)
            failed += 1

        # Commit per request so a later timeout/cancel cannot re-send already
        # delivered supplier messages after rolling back recheck_at clears.
        await session.commit()

    return {
        "status": "ok",
        "processed": processed,
        "skipped": skipped,
        "failed": failed,
    }
