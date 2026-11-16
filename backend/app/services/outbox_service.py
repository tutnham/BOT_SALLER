"""Telegram outbox enqueue and delivery worker."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from loguru import logger
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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


# C1: серия ошибок доставки подряд — алерт операторам каждые N ошибок.
_consecutive_send_errors = 0
_DELIVERY_ERROR_ALERT_EVERY = 5


def consecutive_delivery_errors() -> int:
    """Текущая серия ошибок доставки (для метрик/диагностики)."""
    return _consecutive_send_errors


_UNCERTAIN_MARKERS = (
    "business_peer_usage_missing",
    "recently",
    "timeout",
    "timed out",
)
_PERMANENT_CHAT_MARKERS = (
    "chat not found",
    "bot was kicked",
    "bot is not a member",
    "forbidden: bot was blocked",
    "user is deactivated",
    "peer_id_invalid",
    "chat_id is empty",
)


def _is_uncertain_delivery(kind: str, error_text: str | None) -> bool:
    if kind not in (MessageKind.ask.value, MessageKind.bargain.value):
        return False
    description = (error_text or "").lower()
    return any(marker in description for marker in _UNCERTAIN_MARKERS)


def _is_permanent_delivery_error(error_text: str | None) -> bool:
    description = (error_text or "").lower()
    return any(marker in description for marker in _PERMANENT_CHAT_MARKERS)


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
        if outbound is None:
            raise RuntimeError("outbox_dedupe_message_out_missing")
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
    if outbox is None:
        raise RuntimeError("outbox_row_missing_after_insert")
    outbox.message_out_id = outbound.id
    await session.flush()
    return outbound, True


async def process_outbox_batch(
    session: AsyncSession,
    *,
    telegram: TelegramClientProtocol | None = None,
    worker_id: str | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    on_after_send: Callable[[], Awaitable[None]] | None = None,
) -> int:
    """Claim rows, commit the lease, then send. A crash after send keeps ``processing``."""
    settings = get_settings()
    tg = telegram or get_telegram_client()

    async def _expire_outbox_row(row: TelegramOutbox) -> bool:
        if row.kind in (MessageKind.ask.value, MessageKind.bargain.value):
            row.status = TelegramOutboxStatus.uncertain.value
            row.last_error = "lease_expired"
            row.lease_owner = None
            row.leased_until = None
            await notify_operators(
                session,
                f"⚠️ Истёк lease доставки {row.kind} "
                f"request_id={row.request_id} supplier_id={row.supplier_id}",
                telegram=tg,
            )
            return False
        row.attempts += 1
        row.last_error = "lease_expired"
        if row.attempts >= settings.worker_max_attempts:
            row.status = TelegramOutboxStatus.dead.value
            row.lease_owner = None
            row.leased_until = None
            return False
        return True

    rows = await claim_rows(
        session,
        TelegramOutbox,
        pending_status=TelegramOutboxStatus.pending.value,
        processing_status=TelegramOutboxStatus.processing.value,
        lease_seconds=settings.worker_lease_seconds,
        batch_size=settings.worker_outbox_batch_size,
        worker_id=worker_id,
        on_expired=_expire_outbox_row,
    )
    claimed_ids = [int(row.id) for row in rows]
    await session.commit()
    if not claimed_ids:
        return 0

    sent = 0
    for outbox_id in claimed_ids:
        if session_factory is None:
            sent += await _deliver_outbox_row(
                session,
                outbox_id,
                telegram=tg,
                on_after_send=on_after_send,
            )
            continue
        async with session_factory() as row_session:
            sent += await _deliver_outbox_row(
                row_session,
                outbox_id,
                telegram=tg,
                on_after_send=on_after_send,
            )
    return sent


async def _deliver_outbox_row(
    session: AsyncSession,
    outbox_id: int,
    *,
    telegram: TelegramClientProtocol,
    on_after_send: Callable[[], Awaitable[None]] | None,
) -> int:
    global _consecutive_send_errors
    settings = get_settings()
    row = await session.get(TelegramOutbox, outbox_id)
    if row is None:
        return 0
    outbound = (
        await session.get(MessageOut, row.message_out_id)
        if row.message_out_id is not None
        else None
    )
    try:
        message_id = await telegram.send_message(
            int(row.chat_id),
            row.text,
            parse_mode=row.parse_mode,
            reply_markup=row.reply_markup,
            business_connection_id=row.business_connection_id,
        )
        if on_after_send is not None:
            await on_after_send()
        _consecutive_send_errors = 0
        row.status = TelegramOutboxStatus.sent.value
        row.tg_message_id = message_id
        row.sent_at = datetime.now(UTC)
        row.lease_owner = None
        row.leased_until = None
        if outbound is not None:
            outbound.tg_message_id = message_id
            outbound.send_status = MessageSendStatus.sent.value
            outbound.error_text = None
        await session.commit()
        return 1
    except TelegramSendError as exc:
        await _record_outbox_send_error(
            session,
            row,
            outbound,
            exc.description,
            telegram=telegram,
            settings_max_attempts=settings.worker_max_attempts,
        )
        await session.commit()
        return 0
    except Exception:
        await session.rollback()
        logger.exception("telegram_outbox crashed id={}", outbox_id)
        return 0


async def _record_outbox_send_error(
    session: AsyncSession,
    row: TelegramOutbox,
    outbound: MessageOut | None,
    error_text: str,
    *,
    telegram: TelegramClientProtocol,
    settings_max_attempts: int,
) -> None:
    global _consecutive_send_errors
    _consecutive_send_errors += 1
    if _consecutive_send_errors % _DELIVERY_ERROR_ALERT_EVERY == 0:
        await notify_operators(
            session,
            f"⚠️ Telegram outbox: {_consecutive_send_errors} ошибок доставки подряд\n"
            f"Последняя: {error_text[:300]}",
            telegram=telegram,
        )
    if _is_uncertain_delivery(row.kind, error_text):
        row.status = TelegramOutboxStatus.uncertain.value
        row.last_error = error_text
        row.lease_owner = None
        row.leased_until = None
        if outbound is not None:
            outbound.send_status = MessageSendStatus.failed.value
            outbound.error_text = error_text
        await notify_operators(
            session,
            f"⚠️ Неоднозначная доставка {row.kind} "
            f"request_id={row.request_id} supplier_id={row.supplier_id}\n"
            f"{error_text}",
            telegram=telegram,
        )
        return

    row.attempts += 1
    row.last_error = error_text
    row.lease_owner = None
    row.leased_until = None
    if outbound is not None:
        outbound.send_status = MessageSendStatus.failed.value
        outbound.error_text = error_text
    if _is_permanent_delivery_error(error_text) or row.attempts >= settings_max_attempts:
        row.status = TelegramOutboxStatus.dead.value
    else:
        row.status = TelegramOutboxStatus.pending.value
        row.next_attempt_at = datetime.now(UTC) + timedelta(
            seconds=_backoff_seconds(row.attempts)
        )
