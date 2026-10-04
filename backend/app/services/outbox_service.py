"""Telegram outbox enqueue and delivery worker."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from loguru import logger
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import (
    MessageKind,
    MessageOut,
    MessageSendStatus,
    TelegramOutbox,
    TelegramOutboxStatus,
)
from app.services.alert_service import notify_operators
from app.services.queue_claim import claim_rows
from app.telegram.client import (
    TelegramClientProtocol,
    TelegramSendError,
    get_telegram_client,
)


def _backoff_seconds(attempts: int) -> int:
    return int(min(300, 2 ** min(attempts, 8)))


_UNCERTAIN_MARKERS = (
    "business_peer_usage_missing",
    "recently",
    "timeout",
    "timed out",
)


def _is_uncertain_delivery(kind: str, error_text: str | None) -> bool:
    if kind not in (MessageKind.ask.value, MessageKind.bargain.value):
        return False
    description = (error_text or "").lower()
    return any(marker in description for marker in _UNCERTAIN_MARKERS)


async def enqueue_telegram_message(
    session: AsyncSession,
    *,
    dedupe_key: str,
    chat_id: int,
    text: str,
    kind: MessageKind,
    request_id: int | None,
    supplier_id: int | None,
    business_connection_id: str | None = None,
    parse_mode: str | None = None,
    reply_markup: dict[str, Any] | None = None,
) -> tuple[MessageOut, bool]:
    """
    Queue Telegram delivery. Returns ``(messages_out, is_new_enqueue)``.

    On dedupe conflict returns existing ``messages_out`` linked to the outbox row.
    """
    out_stmt = (
        insert(TelegramOutbox)
        .values(
            dedupe_key=dedupe_key,
            chat_id=chat_id,
            supplier_id=supplier_id,
            request_id=request_id,
            kind=kind.value,
            text=text,
            business_connection_id=business_connection_id,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
            status=TelegramOutboxStatus.pending.value,
        )
        .on_conflict_do_nothing(index_elements=["dedupe_key"])
        .returning(TelegramOutbox.id, TelegramOutbox.message_out_id)
    )
    inserted = (await session.execute(out_stmt)).first()
    await session.flush()

    if inserted is None:
        from sqlalchemy import select

        existing = await session.scalar(
            select(TelegramOutbox).where(TelegramOutbox.dedupe_key == dedupe_key)
        )
        if existing is None or existing.message_out_id is None:
            raise RuntimeError("outbox_dedupe_without_message_out")
        outbound = await session.get(MessageOut, existing.message_out_id)
        assert outbound is not None
        return outbound, False

    outbound = MessageOut(
        request_id=request_id,
        supplier_id=supplier_id,
        tg_message_id=None,
        chat_id=chat_id,
        text=text,
        kind=kind,
        business_connection_id=business_connection_id,
        send_status=MessageSendStatus.pending.value,
        error_text=None,
    )
    session.add(outbound)
    await session.flush()

    outbox_id = inserted[0]
    outbox = await session.get(TelegramOutbox, outbox_id)
    assert outbox is not None
    outbox.message_out_id = outbound.id
    await session.flush()
    return outbound, True


async def process_outbox_batch(
    session: AsyncSession,
    *,
    telegram: TelegramClientProtocol | None = None,
    worker_id: str | None = None,
) -> int:
    """Deliver claimed outbox messages. Returns sent count."""
    settings = get_settings()
    tg = telegram or get_telegram_client()
    rows = await claim_rows(
        session,
        TelegramOutbox,
        pending_status=TelegramOutboxStatus.pending.value,
        processing_status=TelegramOutboxStatus.processing.value,
        lease_seconds=settings.worker_lease_seconds,
        batch_size=settings.worker_outbox_batch_size,
        worker_id=worker_id,
    )
    if not rows:
        return 0

    sent = 0
    for row in rows:
        outbound = (
            await session.get(MessageOut, row.message_out_id)
            if row.message_out_id is not None
            else None
        )
        try:
            message_id = await tg.send_message(
                int(row.chat_id),
                row.text,
                parse_mode=row.parse_mode,
                reply_markup=row.reply_markup,
                business_connection_id=row.business_connection_id,
            )
            row.status = TelegramOutboxStatus.sent.value
            row.tg_message_id = message_id
            row.sent_at = datetime.now(UTC)
            if outbound is not None:
                outbound.tg_message_id = message_id
                outbound.send_status = MessageSendStatus.sent.value
                outbound.error_text = None
            sent += 1
        except TelegramSendError as exc:
            error_text = exc.description
            if _is_uncertain_delivery(row.kind, error_text):
                row.status = TelegramOutboxStatus.uncertain.value
                row.last_error = error_text
                if outbound is not None:
                    outbound.send_status = MessageSendStatus.failed.value
                    outbound.error_text = error_text
                await notify_operators(
                    session,
                    f"⚠️ Неоднозначная доставка {row.kind} "
                    f"request_id={row.request_id} supplier_id={row.supplier_id}\n"
                    f"{error_text}",
                    telegram=tg,
                )
            else:
                row.attempts += 1
                row.last_error = error_text
                if outbound is not None:
                    outbound.send_status = MessageSendStatus.failed.value
                    outbound.error_text = error_text
                if row.attempts >= settings.worker_max_attempts:
                    row.status = TelegramOutboxStatus.dead.value
                else:
                    row.status = TelegramOutboxStatus.pending.value
                    row.next_attempt_at = datetime.now(UTC) + timedelta(
                        seconds=_backoff_seconds(row.attempts)
                    )
        except Exception as exc:
            row.attempts += 1
            row.last_error = f"{type(exc).__name__}: {exc}"[:2000]
            if row.attempts >= settings.worker_max_attempts:
                row.status = TelegramOutboxStatus.dead.value
            else:
                row.status = TelegramOutboxStatus.pending.value
                row.next_attempt_at = datetime.now(UTC) + timedelta(
                    seconds=_backoff_seconds(row.attempts)
                )
            logger.exception("telegram_outbox failed id={}", row.id)
        finally:
            row.lease_owner = None
            row.leased_until = None
            await session.flush()

    return sent
