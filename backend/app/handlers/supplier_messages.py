"""Supplier DM reply handler — regex quotes + group forward (TECH DOC §9.2, Phase 5)."""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

from loguru import logger
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.manual_bind import (
    manual_bind_message as manual_bind_message_use_case,
)
from app.config import get_settings
from app.db.models import (
    MessageIn,
    MessageOut,
    MessageSendStatus,
    Quote,
    QuoteSource,
    Request,
    RequestStatus,
    Supplier,
)
from app.llm.client import (
    LLMProviderError,
    get_llm_client,
    validate_supplier_reply_payload,
)
from app.llm.schemas import ParsedSupplierReply as LlmParsedSupplierReply
from app.parsers.cache import get_cached, set_cached
from app.parsers.regex_parser import parse_supplier_reply
from app.services.alert_service import notify_operators
from app.services.binding_labels import candidate_label
from app.services.candidate_loader_service import load_eligible_candidates
from app.services.price_service import insert_raw_price
from app.services.quote_service import display_price_for_group, upsert_quote
from app.services.reply_binding_service import (
    BindingDecision,
    merge_llm_price,
    resolve_binding,
)
from app.services.route_identity import routes_match
from app.services.routing_service import resolve_supplier_by_chat
from app.services.supplier_bind_session_service import (
    create_bind_session,
    try_handle_supplier_bind_session_reply,
)
from app.services.telemetry import record_supplier_binding_pending
from app.telegram.client import TelegramClientProtocol
from app.telegram.keyboards import inline_keyboard, menu_button
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
    chat_id: int,
    business_connection_id: str | None,
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
            MessageOut.chat_id == chat_id,
            MessageOut.tg_message_id == int(reply_message_id),
            MessageOut.send_status == MessageSendStatus.sent.value,
            MessageOut.request_id.isnot(None),
        )
    )
    if outbound is None or outbound.request_id is None:
        return None
    if not routes_match(outbound.business_connection_id, business_connection_id):
        return None

    result = await session.execute(
        select(Request).where(
            Request.id == outbound.request_id,
            Request.status.in_(_ACTIVE_STATUSES),
        )
    )
    return result.scalar_one_or_none()


async def insert_message_in_idempotent(
    session: AsyncSession,
    *,
    supplier_id: int,
    chat_id: int,
    message_id: int,
    raw_text: str,
    business_connection_id: str | None,
    request_id: int | None,
    bind_method: str | None,
    bind_status: str | None,
    bind_score: float | None,
) -> MessageIn | None:
    values = {
        "request_id": request_id,
        "supplier_id": supplier_id,
        "tg_message_id": message_id,
        "chat_id": chat_id,
        "business_connection_id": business_connection_id,
        "raw_text": raw_text,
        "bind_method": bind_method,
        "bind_status": bind_status,
        "bind_score": bind_score,
    }
    if business_connection_id:
        stmt = (
            insert(MessageIn)
            .values(**values)
            .on_conflict_do_nothing(
                index_elements=["business_connection_id", "chat_id", "tg_message_id"],
                index_where=text("business_connection_id IS NOT NULL"),
            )
            .returning(MessageIn.id)
        )
    else:
        stmt = (
            insert(MessageIn)
            .values(**values)
            .on_conflict_do_nothing(
                index_elements=["chat_id", "tg_message_id"],
                index_where=text("business_connection_id IS NULL"),
            )
            .returning(MessageIn.id)
        )
    new_id = (await session.execute(stmt)).scalar_one_or_none()
    if new_id is None:
        return None
    row = await session.get(MessageIn, new_id)
    await session.flush()
    return row


async def process_bound_supplier_reply(
    session: AsyncSession,
    *,
    request: Request,
    supplier: Supplier,
    raw_text: str,
    message_in: MessageIn,
    telegram: TelegramClientProtocol,
    business_connection_id: str | None,
    chat_id: int,
    bind_method: str,
    binding_price: Decimal | None = None,
) -> Quote | None:
    """Parse supplier text, upsert quote, notify employee group. Returns the quote."""
    message_in.bind_method = bind_method
    message_in.bind_status = "bound"
    message_in.request_id = request.id
    await session.flush()

    if request.status in (RequestStatus.closed, RequestStatus.cancelled):
        await telegram.send_message(
            int(chat_id),
            render_template("request_not_open", request_id=request.id),
            business_connection_id=business_connection_id,
        )
        return None

    from app.parsers.price_parse import parse_price_from_text

    price_result = parse_price_from_text(raw_text)
    parsed: Any = parse_supplier_reply(raw_text)
    if price_result.price is not None:
        parsed = parsed.model_copy(
            update={
                "price": price_result.price,
                "confidence": max(parsed.confidence, price_result.confidence),
            }
        )
    elif price_result.validation_errors and message_in.bind_status == "bound":
        from app.services.supplier_review_service import ensure_review_item_for_message

        message_in.bind_status = "pending_binding"
        await ensure_review_item_for_message(
            session,
            message_in=message_in,
            conflict_reason="price_untrusted",
        )
        await session.flush()
        return None

    source = QuoteSource.regex
    if parsed.price is None and parsed.qty is None:
        parsed = await _parse_with_llm_cache(session, raw_text)
        source = QuoteSource.llm
    parsed = merge_llm_price(parsed, binding_price)

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
    previous_price_final = display_price_for_group(existing_quote)

    from app.services.supplier_correction_service import (
        classify_supplier_followup,
        record_quote_correction,
    )

    followup = await classify_supplier_followup(
        session,
        supplier_id=supplier.id,
        request_id=request.id,
        new_price=parsed.price,
        new_text=raw_text,
        message_in_id=message_in.id,
    )
    if followup == "duplicate" and existing_quote is not None:
        return existing_quote

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

    if quote is not None and followup == "correction" and parsed.price is not None:
        await record_quote_correction(
            session,
            quote_id=quote.id,
            old_price=previous_price_initial,
            new_price=parsed.price,
            message_in_id=message_in.id,
        )
        from app.services.alert_service import notify_operators

        await notify_operators(
            session,
            render_template(
                "supplier_price_corrected_employee",
                request_id=request.id,
                old_price=previous_price_initial,
                new_price=parsed.price,
                client_old=previous_price_final,
                client_new=display_price_for_group(quote),
            ),
            telegram=telegram,
        )

    if quote is not None and followup != "duplicate":
        forward_text = render_template(
            "supplier_quote_parsed",
            request_id=request.id,
            available=parsed.available,
            price=display_price_for_group(quote),
            qty=parsed.qty,
        )
    else:
        forward_text = render_template(
            "supplier_low_confidence",
            request_id=request.id,
            raw_text=raw_text,
        )

    if followup != "duplicate":
        await telegram.send_message(request.group_chat_id, forward_text)

    if (
        quote is not None
        and request.status is RequestStatus.needs_recheck
        and parsed.price is not None
        and previous_price_initial is not None
        and parsed.price != previous_price_initial
    ):
        new_final = display_price_for_group(quote)
        if (
            previous_price_final is not None
            and new_final is not None
            and previous_price_final != new_final
        ):
            await telegram.send_message(
                request.group_chat_id,
                render_template(
                    "price_changed",
                    request_id=request.id,
                    old_price=previous_price_final,
                    new_price=new_final,
                ),
            )
    return quote


async def _notify_unbound(
    session: AsyncSession,
    *,
    message_in: MessageIn,
    supplier: Supplier,
    raw_text: str,
    candidates: list[Request],
    telegram: TelegramClientProtocol,
) -> None:
    rows: list[list[dict[str, Any]]] = []
    if not candidates:
        block = "—"
    else:
        lines: list[str] = []
        for request in candidates[:8]:
            label = candidate_label(request)
            lines.append(f"#{request.id} {label}")
            rows.append(
                [
                    menu_button(
                        f"#{request.id} {label}"[:40],
                        "bind_pick",
                        message_in.id,
                        page=request.id,
                    )
                ]
            )
        block = "\n".join(lines)
    rows.append([menu_button("Игнорировать", "bind_ignore", message_in.id)])
    markup = inline_keyboard(rows)
    await notify_operators(
        session,
        render_template(
            "alert_unbound_supplier_message",
            supplier_name=supplier.name,
            supplier_id=supplier.id,
            raw_text=raw_text[:500],
            candidates_block=block,
        ),
        telegram=telegram,
        reply_markup=markup,
    )


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

    if await try_handle_supplier_bind_session_reply(
        session,
        supplier=supplier,
        raw_text=raw_text,
        chat_id=int(chat_id),
        message_id=int(message_id),
        telegram=telegram,
        business_connection_id=business_connection_id,
    ):
        return "ok"

    price_match = _PRICE_LIST_MARKER_RE.match(raw_text.strip())
    if price_match is not None:
        price_body = (price_match.group(1) or "").strip() or raw_text.strip()
        row = await insert_message_in_idempotent(
            session,
            supplier_id=supplier.id,
            chat_id=int(chat_id),
            message_id=int(message_id),
            raw_text=raw_text,
            business_connection_id=business_connection_id,
            request_id=None,
            bind_method="none",
            bind_status="ignored",
            bind_score=None,
        )
        if row is None:
            return "ok"
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

    reply_request = await _resolve_request_from_outbound_reply(
        session,
        supplier_id=supplier.id,
        message=message,
        chat_id=int(chat_id),
        business_connection_id=business_connection_id,
    )

    from app.services.supplier_batch_service import (
        is_multiline_rfq_candidate,
        process_batch_supplier_reply,
    )

    decision: BindingDecision = await resolve_binding(
        session,
        supplier=supplier,
        message=message,
        raw_text=raw_text,
        reply_request=reply_request,
        chat_id=int(chat_id),
        business_connection_id=business_connection_id,
    )

    bind_status = decision.status
    if bind_status == "bound":
        bind_method = decision.method
    elif bind_status == "pending_binding":
        bind_method = "none"
        bind_status = "pending_binding"
    else:
        bind_method = "none"

    message_in = await insert_message_in_idempotent(
        session,
        supplier_id=supplier.id,
        chat_id=int(chat_id),
        message_id=int(message_id),
        raw_text=raw_text,
        business_connection_id=business_connection_id,
        request_id=decision.request.id if decision.request else None,
        bind_method=bind_method,
        bind_status=bind_status,
        bind_score=decision.score,
    )
    if message_in is None:
        logger.info(
            "supplier_message duplicate chat_id={} message_id={}",
            chat_id,
            message_id,
        )
        return "ok"

    if get_settings().supplier_batch_reply_enabled and is_multiline_rfq_candidate(raw_text):
        batch_result = await process_batch_supplier_reply(
            session,
            supplier=supplier,
            raw_text=raw_text,
            message_in=message_in,
            chat_id=int(chat_id),
            business_connection_id=business_connection_id,
            telegram=telegram,
        )
        if batch_result == "ok":
            return "ok"

    if decision.status == "ignored":
        logger.info(
            "supplier_message ignored supplier_id={} reason={}",
            supplier.id,
            decision.ignored_reason,
        )
        return "ok"

    if decision.request is None:
        candidates = decision.candidates
        if not candidates:
            candidates = await load_eligible_candidates(
                session,
                supplier=supplier,
                chat_id=int(chat_id),
                business_connection_id=business_connection_id,
            )
        if not candidates:
            logger.info(
                "supplier_message unbound silent supplier_id={} no open requests",
                supplier.id,
            )
            return "ok"
        from app.services.supplier_review_service import ensure_review_item_for_message

        await ensure_review_item_for_message(
            session,
            message_in=message_in,
            conflict_reason=decision.conflict_reason,
            candidate_request_ids=[r.id for r in candidates],
        )
        if decision.status == "pending_binding" or decision.needs_supplier_confirm:
            record_supplier_binding_pending()
            bind_session = await create_bind_session(
                session,
                supplier=supplier,
                message_in=message_in,
                candidates=candidates,
                raw_text=raw_text,
                telegram=telegram,
                business_connection_id=business_connection_id,
                use_yes_no_fallback=decision.needs_supplier_confirm,
            )
            if bind_session is not None:
                message_in.bind_status = "pending_binding"
                await session.flush()
            await _notify_unbound(
                session,
                message_in=message_in,
                supplier=supplier,
                raw_text=raw_text,
                candidates=candidates,
                telegram=telegram,
            )
            return "ok"
        await _notify_unbound(
            session,
            message_in=message_in,
            supplier=supplier,
            raw_text=raw_text,
            candidates=candidates,
            telegram=telegram,
        )
        return "ok"

    await process_bound_supplier_reply(
        session,
        request=decision.request,
        supplier=supplier,
        raw_text=raw_text,
        message_in=message_in,
        telegram=telegram,
        business_connection_id=business_connection_id,
        chat_id=int(chat_id),
        bind_method=decision.method,
        binding_price=decision.binding_price,
    )
    return "ok"


async def _withdraw_quote(
    session: AsyncSession,
    *,
    message_in: MessageIn,
    supplier: Supplier,
    telegram: TelegramClientProtocol,
) -> None:
    """Remove the quote this message produced from its previous request.

    Only when this message is the supplier's latest bound message for that
    request and the quote is not manual — newer prices stay untouched.
    """
    old_request_id = message_in.request_id
    if old_request_id is None:
        return
    latest_id = await session.scalar(
        select(func.max(MessageIn.id)).where(
            MessageIn.request_id == old_request_id,
            MessageIn.supplier_id == supplier.id,
            MessageIn.bind_status == "bound",
        )
    )
    if latest_id != message_in.id:
        return
    quote = await session.scalar(
        select(Quote).where(
            Quote.request_id == old_request_id,
            Quote.supplier_id == supplier.id,
        )
    )
    if quote is None or quote.source == QuoteSource.manual:
        return
    old_request = await session.get(Request, old_request_id)
    await session.delete(quote)
    await session.flush()
    if old_request is None:
        return
    await telegram.send_message(
        old_request.group_chat_id,
        render_template("client_quote_withdrawn", request_id=old_request_id),
    )
    remaining = await session.scalar(
        select(func.count()).select_from(Quote).where(Quote.request_id == old_request_id)
    )
    if not remaining and old_request.status is RequestStatus.priced:
        old_request.status = RequestStatus.awaiting_answers
        await session.flush()


async def manual_bind_message(
    session: AsyncSession,
    *,
    message_in_id: int,
    request_id: int,
    telegram: TelegramClientProtocol,
) -> str:
    """Owner manual bind of supplier message; moves the quote when rebinding."""
    return await manual_bind_message_use_case(
        session,
        message_in_id=message_in_id,
        request_id=request_id,
        telegram=telegram,
    )


async def unbind_message(
    session: AsyncSession,
    *,
    message_in_id: int,
    telegram: TelegramClientProtocol,
    status: str = "unbound",
) -> str:
    """Detach supplier message from its request and withdraw the quote it made."""
    message_in = await session.get(MessageIn, message_in_id)
    if message_in is None:
        return "not_found"
    supplier = await session.get(Supplier, message_in.supplier_id)
    if supplier is None:
        return "supplier_not_found"

    if message_in.request_id is not None:
        await _withdraw_quote(
            session, message_in=message_in, supplier=supplier, telegram=telegram
        )
    message_in.request_id = None
    message_in.bind_method = "none"
    message_in.bind_status = status
    await session.flush()
    return "ok"
