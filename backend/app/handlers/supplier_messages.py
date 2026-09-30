"""Supplier DM reply handler — regex quotes + group forward (TECH DOC §9.2, Phase 5)."""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import (
    MessageIn,
    MessageOut,
    MessageSendStatus,
    Quote,
    QuoteSource,
    Request,
    RequestStatus,
)
from app.llm.client import (
    LLMProviderError,
    get_llm_client,
    validate_supplier_reply_payload,
)
from app.llm.schemas import ParsedSupplierReply as LlmParsedSupplierReply
from app.parsers.cache import get_cached, set_cached
from app.parsers.regex_parser import parse_supplier_reply
from app.services.price_service import insert_raw_price
from app.services.quote_service import upsert_quote
from app.services.routing_service import resolve_supplier_by_chat
from app.telegram.client import TelegramClientProtocol
from app.templates.messages_ru import render_template
from app.utils.whitelist import get_supplier_by_telegram_id

_PRICE_LIST_MARKER_RE = re.compile(
    r"^(?:/price|#?прайс)\b[\s:,-]*(.*)$",
    re.IGNORECASE | re.DOTALL,
)

_ACTIVE_STATUSES = (
    RequestStatus.awaiting_answers,
    RequestStatus.bargaining,
    RequestStatus.needs_recheck,
    RequestStatus.priced,
    RequestStatus.open,
)


async def _parse_with_llm_cache(
    session: AsyncSession,
    raw_text: str,
) -> LlmParsedSupplierReply:
    kind = "supplier_reply"
    cached = await get_cached(session, kind=kind, raw_text=raw_text)
    if cached is not None:
        return validate_supplier_reply_payload(cached)

    llm = get_llm_client()
    try:
        parsed = await llm.parse_supplier_reply(raw_text)
    except LLMProviderError:
        return validate_supplier_reply_payload({})

    settings = get_settings()
    model_used = settings.llm_model or settings.llm_provider
    await set_cached(
        session,
        kind=kind,
        raw_text=raw_text,
        result_json=parsed.model_dump(mode="json"),
        model_used=model_used,
    )
    return parsed


async def _resolve_request_from_outbound_reply(
    session: AsyncSession,
    *,
    supplier_id: int,
    message: dict,
) -> Request | None:
    """Bind supplier reply only to a prior bot ``messages_out`` row (strict reply)."""
    reply_to = message.get("reply_to_message")
    if not reply_to:
        return None
    reply_message_id = reply_to.get("message_id")
    if reply_message_id is None:
        return None

    outbound = await session.scalar(
        select(MessageOut).where(
            MessageOut.supplier_id == supplier_id,
            MessageOut.tg_message_id == int(reply_message_id),
            MessageOut.send_status == MessageSendStatus.sent.value,
            MessageOut.request_id.isnot(None),
        )
    )
    if outbound is None or outbound.request_id is None:
        return None

    result = await session.execute(
        select(Request).where(
            Request.id == outbound.request_id,
            Request.status.in_(_ACTIVE_STATUSES),
        )
    )
    return result.scalar_one_or_none()


async def handle_reply(
    session: AsyncSession,
    message: dict,
    *,
    telegram: TelegramClientProtocol,
    business_connection_id: str | None = None,
) -> str:
    """
    Process supplier private message: bind request, log ``messages_in``, parse regex.

    Returns webhook status ``ok``.
    """
    from_user = message.get("from") or {}
    telegram_id = from_user.get("id")
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    if chat_id is None:
        return "ignored"

    chat_type = chat.get("type")
    if chat_type == "private":
        if telegram_id is None:
            return "ignored"
        supplier = await get_supplier_by_telegram_id(session, int(telegram_id))
    else:
        supplier = await resolve_supplier_by_chat(session, int(chat_id))

    if supplier is None:
        return "ignored"

    message_id = message.get("message_id")
    max_len = get_settings().max_message_text_len
    raw_text = (message.get("text") or message.get("caption") or "")[:max_len]
    if chat_id is None or message_id is None:
        return "ignored"

    price_match = _PRICE_LIST_MARKER_RE.match(raw_text.strip())
    if price_match is not None:
        price_body = (price_match.group(1) or "").strip() or raw_text.strip()
        session.add(
            MessageIn(
                request_id=None,
                supplier_id=supplier.id,
                tg_message_id=int(message_id),
                chat_id=int(chat_id),
                business_connection_id=business_connection_id,
                raw_text=raw_text,
            )
        )
        await session.flush()
        await insert_raw_price(
            session,
            supplier_id=supplier.id,
            text=price_body,
            source="manual_message",
        )
        await telegram.send_message(
            int(chat_id),
            render_template("price_received"),
            business_connection_id=business_connection_id,
        )
        return "ok"

    request = await _resolve_request_from_outbound_reply(
        session,
        supplier_id=supplier.id,
        message=message,
    )

    session.add(
        MessageIn(
            request_id=request.id if request else None,
            supplier_id=supplier.id,
            tg_message_id=int(message_id),
            chat_id=int(chat_id),
            business_connection_id=business_connection_id,
            raw_text=raw_text,
        )
    )
    await session.flush()

    if request is None:
        # Ordinary supplier DM/group chatter is not an RFQ reply. Stay silent:
        # sending supplier_need_reply into Business personal chats floods the
        # owner's conversation with the supplier.
        return "ok"

    if request.status in (RequestStatus.closed, RequestStatus.cancelled):
        await telegram.send_message(
            int(chat_id),
            render_template("request_not_open", request_id=request.id),
            business_connection_id=business_connection_id,
        )
        return "ok"

    parsed: Any = parse_supplier_reply(raw_text)
    source = QuoteSource.regex
    if parsed.price is None and parsed.qty is None:
        parsed = await _parse_with_llm_cache(session, raw_text)
        source = QuoteSource.llm

    # Snapshot price_initial before upsert for recheck change notification (§9.4).
    existing = await session.execute(
        select(Quote).where(
            Quote.request_id == request.id,
            Quote.supplier_id == supplier.id,
        )
    )
    existing_quote = existing.scalar_one_or_none()
    previous_price_initial = (
        existing_quote.price_initial if existing_quote is not None else None
    )

    # Bargaining replies write price_bargain only; leave price_initial intact (§9.3).
    is_bargain = request.status is RequestStatus.bargaining
    quote = await upsert_quote(
        session,
        request.id,
        supplier.id,
        price=parsed.price,
        qty=parsed.qty,
        available=parsed.available,
        condition=parsed.condition,
        source=source,
        confidence=parsed.confidence,
        bargain=is_bargain,
    )

    if quote is not None:
        forward_text = render_template(
            "supplier_quote_parsed",
            request_id=request.id,
            available=parsed.available,
            price=parsed.price,
            qty=parsed.qty,
        )
    else:
        forward_text = render_template(
            "supplier_low_confidence",
            request_id=request.id,
            raw_text=raw_text,
        )

    await telegram.send_message(request.group_chat_id, forward_text)

    # Recheck price-change notify: compare against previous price_initial only.
    if (
        quote is not None
        and request.status is RequestStatus.needs_recheck
        and parsed.price is not None
        and previous_price_initial is not None
        and parsed.price != previous_price_initial
    ):
        await telegram.send_message(
            request.group_chat_id,
            render_template(
                "price_changed",
                request_id=request.id,
                old_price=previous_price_initial,
                new_price=parsed.price,
            ),
        )

    return "ok"
