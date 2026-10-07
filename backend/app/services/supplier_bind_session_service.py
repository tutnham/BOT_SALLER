"""Supplier bind sessions for ambiguous inbound prices (one session per MessageIn)."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import (
    MessageIn,
    Request,
    Supplier,
    SupplierBindSession,
    SupplierBindSessionStatus,
)
from app.parsers.product_normalizer import extract_product_attrs
from app.parsers.regex_parser import parse_supplier_reply
from app.services.binding_labels import candidate_label
from app.services.explicit_request_parser import parse_explicit_request_id
from app.services.reply_binding_service import (
    attrs_match_score,
    source_text_overlap_score,
)
from app.services.route_identity import routes_match
from app.services.supplier_bind_prompt_service import (
    is_affirmative_reply,
    is_negative_reply,
    request_preview_text,
)
from app.telegram.client import TelegramClientProtocol
from app.telegram.keyboards import CallbackData, button, inline_keyboard
from app.templates.messages_ru import render_template

_AFFIRMATIVE_RE = re.compile(
    r"^(?:да|ага|угу|\+|yes|yep|ок|ok|okay)[\s!.]*$",
    re.IGNORECASE,
)
_NEGATIVE_RE = re.compile(
    r"^(?:нет|не|no|нету|неа|\-|—)[\s!.]*$",
    re.IGNORECASE,
)

ResolveResult = Literal[
    "ok",
    "not_found",
    "forbidden",
    "expired",
    "already_resolved",
    "invalid_candidate",
    "not_pending",
]


def _session_expired(row: SupplierBindSession) -> bool:
    return row.expires_at < datetime.now(UTC)


async def _lock_session(
    session: AsyncSession,
    session_id: int,
) -> SupplierBindSession | None:
    result = await session.execute(
        select(SupplierBindSession)
        .where(SupplierBindSession.id == session_id)
        .with_for_update()
    )
    return result.scalar_one_or_none()


async def create_bind_session(
    session: AsyncSession,
    *,
    supplier: Supplier,
    message_in: MessageIn,
    candidates: list[Request],
    raw_text: str,
    telegram: TelegramClientProtocol,
    business_connection_id: str | None,
    use_yes_no_fallback: bool = False,
) -> SupplierBindSession | None:
    if not candidates:
        return None
    ordered = _rank_candidates(raw_text=raw_text, candidates=candidates)
    candidate_ids = [request.id for request in ordered]
    ttl = get_settings().supplier_bind_session_ttl_hours
    expires_at = datetime.now(UTC) + timedelta(hours=ttl)
    current_id = ordered[0].id
    row = SupplierBindSession(
        supplier_id=supplier.id,
        message_in_id=message_in.id,
        chat_id=message_in.chat_id,
        business_connection_id=business_connection_id,
        candidate_request_ids=candidate_ids,
        current_candidate_id=current_id,
        status=SupplierBindSessionStatus.pending.value,
        expires_at=expires_at,
    )
    session.add(row)
    await session.flush()
    await _send_supplier_prompt(
        session,
        bind_session=row,
        supplier=supplier,
        candidates=ordered,
        raw_text=raw_text,
        telegram=telegram,
        business_connection_id=business_connection_id,
        use_yes_no_fallback=use_yes_no_fallback,
    )
    return row


def _rank_candidates(*, raw_text: str, candidates: list[Request]) -> list[Request]:
    message_attrs = extract_product_attrs(raw_text)
    scored: list[tuple[Request, int]] = []
    for request in candidates:
        score = attrs_match_score(message_attrs, request.normalized_json)
        if score < 0:
            continue
        score += source_text_overlap_score(raw_text, request.source_text or "")
        scored.append((request, score))
    if not scored:
        scored = [(request, 0) for request in candidates]
        scored.sort(key=lambda item: item[0].id)
        return [request for request, _ in scored]
    scored.sort(key=lambda item: (item[1], -item[0].id), reverse=True)
    return [request for request, _ in scored]


async def _send_supplier_prompt(
    session: AsyncSession,
    *,
    bind_session: SupplierBindSession,
    supplier: Supplier,
    candidates: list[Request],
    raw_text: str,
    telegram: TelegramClientProtocol,
    business_connection_id: str | None,
    use_yes_no_fallback: bool,
) -> None:
    if not get_settings().supplier_bind_ask_enabled:
        return
    settings = get_settings()
    if use_yes_no_fallback and len(candidates) == 1:
        await telegram.send_message(
            bind_session.chat_id,
            render_template(
                "supplier_bind_confirm",
                request_text=request_preview_text(candidates[0]),
            ),
            business_connection_id=business_connection_id,
        )
        return

    parsed = parse_supplier_reply(raw_text)
    price_text = str(parsed.price) if parsed.price is not None else raw_text.strip()
    lines = [f"#{request.id} {candidate_label(request)}" for request in candidates[:8]]
    first_id = candidates[0].id
    text = render_template(
        "supplier_bind_ambiguous",
        price=price_text,
        candidate_lines="\n".join(lines),
        first_request_id=first_id,
    )
    markup = None
    if settings.supplier_bind_buttons_enabled:
        rows: list[list[dict]] = []
        for request in candidates[:6]:
            label = f"#{request.id} {candidate_label(request)}"[:40]
            rows.append(
                [
                    button(
                        label,
                        cd=CallbackData(
                            namespace="sup",
                            action="bind_pick",
                            arg=bind_session.id,
                            page=request.id,
                        ),
                    )
                ]
            )
        rows.append(
            [
                button(
                    "Другая / не относится",
                    cd=CallbackData(
                        namespace="sup",
                        action="bind_other",
                        arg=bind_session.id,
                        page=0,
                    ),
                )
            ]
        )
        markup = inline_keyboard(rows)
    await telegram.send_message(
        bind_session.chat_id,
        text,
        reply_markup=markup,
        business_connection_id=business_connection_id,
    )


async def resolve_bind_session(
    session: AsyncSession,
    *,
    session_id: int,
    request_id: int,
    resolution_method: str,
    supplier: Supplier,
    chat_id: int,
    business_connection_id: str | None,
    resolved_by_telegram_user_id: int | None,
    telegram: TelegramClientProtocol,
) -> ResolveResult:
    bind_session = await _lock_session(session, session_id)
    if bind_session is None:
        return "not_found"
    if bind_session.supplier_id != supplier.id:
        return "forbidden"
    if bind_session.chat_id != chat_id or not routes_match(
        bind_session.business_connection_id, business_connection_id
    ):
        return "forbidden"
    if bind_session.status != SupplierBindSessionStatus.pending.value:
        if (
            bind_session.status == SupplierBindSessionStatus.resolved.value
            and bind_session.resolved_request_id == request_id
        ):
            return "already_resolved"
        return "not_pending"
    if _session_expired(bind_session):
        bind_session.status = SupplierBindSessionStatus.expired.value
        bind_session.resolved_at = datetime.now(UTC)
        await session.flush()
        return "expired"
    allowed = set(bind_session.candidate_request_ids or [])
    if request_id not in allowed:
        return "invalid_candidate"

    message_in = await session.get(MessageIn, bind_session.message_in_id)
    if message_in is None:
        return "not_found"
    if message_in.bind_status == "bound" and message_in.request_id == request_id:
        bind_session.status = SupplierBindSessionStatus.resolved.value
        bind_session.resolved_request_id = request_id
        bind_session.resolution_method = resolution_method
        bind_session.resolved_at = datetime.now(UTC)
        await session.flush()
        return "already_resolved"

    request = await session.get(Request, request_id)
    if request is None:
        return "invalid_candidate"

    from app.application.binding import RequestNotBindableError, assert_request_bindable

    try:
        assert_request_bindable(request)
    except RequestNotBindableError:
        return "invalid_candidate"

    bind_session.status = SupplierBindSessionStatus.resolved.value
    bind_session.resolved_request_id = request_id
    bind_session.resolution_method = resolution_method
    bind_session.resolved_by_telegram_user_id = resolved_by_telegram_user_id
    bind_session.resolved_at = datetime.now(UTC)
    await session.flush()

    from app.handlers.supplier_messages import process_bound_supplier_reply

    await process_bound_supplier_reply(
        session,
        request=request,
        supplier=supplier,
        raw_text=message_in.raw_text,
        message_in=message_in,
        telegram=telegram,
        business_connection_id=bind_session.business_connection_id,
        chat_id=bind_session.chat_id,
        bind_method=resolution_method,
    )
    return "ok"


async def cancel_bind_sessions_for_message(
    session: AsyncSession,
    *,
    message_in_id: int,
    resolution_method: str = "manual",
) -> None:
    result = await session.execute(
        select(SupplierBindSession)
        .where(
            SupplierBindSession.message_in_id == message_in_id,
            SupplierBindSession.status == SupplierBindSessionStatus.pending.value,
        )
        .with_for_update()
    )
    now = datetime.now(UTC)
    for row in result.scalars().all():
        row.status = SupplierBindSessionStatus.cancelled.value
        row.resolution_method = resolution_method
        row.resolved_at = now
    await session.flush()


async def try_handle_supplier_bind_session_reply(
    session: AsyncSession,
    *,
    supplier: Supplier,
    raw_text: str,
    chat_id: int,
    message_id: int,
    telegram: TelegramClientProtocol,
    business_connection_id: str | None,
) -> bool:
    """Handle explicit #N, yes/no fallback, or sequential deny for pending sessions."""
    explicit_id = parse_explicit_request_id(raw_text)
    if explicit_id is not None:
        pending = await _pending_sessions_for_supplier(
            session, supplier_id=supplier.id, chat_id=chat_id, business_connection_id=business_connection_id
        )
        for bind_session in pending:
            if explicit_id in set(bind_session.candidate_request_ids or []):
                result = await resolve_bind_session(
                    session,
                    session_id=bind_session.id,
                    request_id=explicit_id,
                    resolution_method="explicit_request_id",
                    supplier=supplier,
                    chat_id=chat_id,
                    business_connection_id=business_connection_id,
                    resolved_by_telegram_user_id=supplier.telegram_id,
                    telegram=telegram,
                )
                if result in ("ok", "already_resolved"):
                    await _insert_ack_message(
                        session,
                        supplier_id=supplier.id,
                        chat_id=chat_id,
                        message_id=message_id,
                        raw_text=raw_text,
                        business_connection_id=business_connection_id,
                    )
                    return True
        return False

    pending = await _pending_sessions_for_supplier(
        session, supplier_id=supplier.id, chat_id=chat_id, business_connection_id=business_connection_id
    )
    if not pending:
        return False
    bind_session = pending[0]
    parsed = parse_supplier_reply(raw_text)
    if parsed.price is not None and not is_affirmative_reply(raw_text):
        return False

    if is_negative_reply(raw_text):
        await _advance_yes_no_session(session, bind_session=bind_session, supplier=supplier, telegram=telegram)
        await _insert_ack_message(
            session,
            supplier_id=supplier.id,
            chat_id=chat_id,
            message_id=message_id,
            raw_text=raw_text,
            business_connection_id=business_connection_id,
        )
        return True

    if not is_affirmative_reply(raw_text):
        return False
    if bind_session.current_candidate_id is None:
        return False
    result = await resolve_bind_session(
        session,
        session_id=bind_session.id,
        request_id=int(bind_session.current_candidate_id),
        resolution_method="confirm",
        supplier=supplier,
        chat_id=chat_id,
        business_connection_id=business_connection_id,
        resolved_by_telegram_user_id=supplier.telegram_id,
        telegram=telegram,
    )
    if result in ("ok", "already_resolved"):
        await _insert_ack_message(
            session,
            supplier_id=supplier.id,
            chat_id=chat_id,
            message_id=message_id,
            raw_text=raw_text,
            business_connection_id=business_connection_id,
        )
        return True
    return False


async def _pending_sessions_for_supplier(
    session: AsyncSession,
    *,
    supplier_id: int,
    chat_id: int,
    business_connection_id: str | None,
) -> list[SupplierBindSession]:
    result = await session.execute(
        select(SupplierBindSession)
        .where(
            SupplierBindSession.supplier_id == supplier_id,
            SupplierBindSession.chat_id == chat_id,
            SupplierBindSession.status == SupplierBindSessionStatus.pending.value,
        )
        .order_by(SupplierBindSession.created_at.asc())
    )
    rows = list(result.scalars().all())
    active: list[SupplierBindSession] = []
    for row in rows:
        if not routes_match(row.business_connection_id, business_connection_id):
            continue
        if _session_expired(row):
            row.status = SupplierBindSessionStatus.expired.value
            row.resolved_at = datetime.now(UTC)
            continue
        active.append(row)
    await session.flush()
    return active


async def _advance_yes_no_session(
    session: AsyncSession,
    *,
    bind_session: SupplierBindSession,
    supplier: Supplier,
    telegram: TelegramClientProtocol,
) -> None:
    pending_ids = list(bind_session.candidate_request_ids or [])
    current = bind_session.current_candidate_id
    if current is not None and current in pending_ids:
        pending_ids = [item for item in pending_ids if item != current]
    if not pending_ids:
        bind_session.status = SupplierBindSessionStatus.ignored.value
        bind_session.resolved_at = datetime.now(UTC)
        await session.flush()
        if get_settings().supplier_bind_ask_enabled:
            await telegram.send_message(
                bind_session.chat_id,
                render_template("supplier_bind_gave_up"),
                business_connection_id=bind_session.business_connection_id,
            )
        return
    next_id = int(pending_ids[0])
    bind_session.current_candidate_id = next_id
    bind_session.candidate_request_ids = pending_ids
    await session.flush()
    request = await session.get(Request, next_id)
    if request is None:
        return
    if get_settings().supplier_bind_ask_enabled:
        await telegram.send_message(
            bind_session.chat_id,
            render_template("supplier_bind_confirm", request_text=request_preview_text(request)),
            business_connection_id=bind_session.business_connection_id,
        )


async def handle_supplier_bind_callback(
    session: AsyncSession,
    *,
    callback_query: dict,
    telegram: TelegramClientProtocol,
) -> str:
    from_user = callback_query.get("from") or {}
    telegram_id = from_user.get("id")
    if telegram_id is None:
        return "ignored"
    from app.utils.whitelist import get_supplier_by_telegram_id

    supplier = await get_supplier_by_telegram_id(session, int(telegram_id))
    if supplier is None:
        return "ignored"

    message = callback_query.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    if chat_id is None:
        return "ignored"
    business_connection_id = message.get("business_connection_id")

    cd = CallbackData.decode(callback_query.get("data"))
    callback_id = callback_query.get("id", "")
    if cd is None or cd.namespace != "sup":
        return "ignored"

    if cd.action == "bind_other":
        bind_session = await _lock_session(session, cd.arg)
        if bind_session is not None and bind_session.status == SupplierBindSessionStatus.pending.value:
            bind_session.status = SupplierBindSessionStatus.ignored.value
            bind_session.resolution_method = "supplier_other"
            bind_session.resolved_at = datetime.now(UTC)
            await session.flush()
        await telegram.answer_callback_query(callback_id)
        return "ok"

    if cd.action != "bind_pick":
        return "ignored"

    result = await resolve_bind_session(
        session,
        session_id=cd.arg,
        request_id=cd.page,
        resolution_method="callback",
        supplier=supplier,
        chat_id=int(chat_id),
        business_connection_id=business_connection_id,
        resolved_by_telegram_user_id=int(telegram_id),
        telegram=telegram,
    )
    if result == "ok":
        await telegram.answer_callback_query(callback_id, text="Привязано")
    elif result == "already_resolved":
        await telegram.answer_callback_query(callback_id)
    else:
        await telegram.answer_callback_query(callback_id, text="Не удалось привязать", show_alert=True)
    return "ok"


async def _insert_ack_message(
    session: AsyncSession,
    *,
    supplier_id: int,
    chat_id: int,
    message_id: int,
    raw_text: str,
    business_connection_id: str | None,
) -> None:
    from app.handlers.supplier_messages import insert_message_in_idempotent

    await insert_message_in_idempotent(
        session,
        supplier_id=supplier_id,
        chat_id=chat_id,
        message_id=message_id,
        raw_text=raw_text,
        business_connection_id=business_connection_id,
        request_id=None,
        bind_method="none",
        bind_status="ignored",
        bind_score=None,
    )
