"""Supplier confirmation when price arrives without reply and binding is ambiguous."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from loguru import logger
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import MessageIn, Quote, Request, Supplier, SupplierBindPrompt
from app.parsers.product_normalizer import ProductAttrs
from app.parsers.regex_parser import parse_supplier_reply
from app.services.reply_binding_service import (
    attrs_match_score,
    source_text_overlap_score,
)
from app.telegram.client import TelegramClientProtocol
from app.templates.messages_ru import render_template

_AFFIRMATIVE_RE = re.compile(
    r"^(?:да|ага|угу|\+|yes|yep|ок|ok|okay)[\s!.]*$",
    re.IGNORECASE,
)
_NEGATIVE_RE = re.compile(
    r"^(?:нет|не|no|нету|неа|\-|—)[\s!.]*$",
    re.IGNORECASE,
)

_REQUEST_TEXT_PREVIEW_LEN = 200


def is_affirmative_reply(raw_text: str) -> bool:
    return bool(_AFFIRMATIVE_RE.match((raw_text or "").strip()))


def is_negative_reply(raw_text: str) -> bool:
    return bool(_NEGATIVE_RE.match((raw_text or "").strip()))


def rank_candidates_for_confirm(
    *,
    raw_text: str,
    message_attrs: ProductAttrs,
    candidates: list[Request],
) -> list[Request]:
    scored: list[tuple[Request, int]] = []
    for request in candidates:
        score = attrs_match_score(message_attrs, request.normalized_json)
        if score < 0:
            continue
        score += source_text_overlap_score(raw_text, request.source_text or "")
        scored.append((request, score))
    if not scored:
        scored = [(request, 0) for request in candidates]
    else:
        scored.sort(key=lambda item: (item[1], -item[0].id), reverse=True)
        return [request for request, _score in scored]
    scored.sort(key=lambda item: item[0].id)
    return [request for request, _score in scored]


def request_preview_text(request: Request) -> str:
    text = (request.source_text or "").strip()
    if len(text) > _REQUEST_TEXT_PREVIEW_LEN:
        return text[: _REQUEST_TEXT_PREVIEW_LEN - 1] + "…"
    return text


async def clear_supplier_bind_prompt(
    session: AsyncSession,
    *,
    supplier_id: int,
) -> None:
    await session.execute(
        delete(SupplierBindPrompt).where(SupplierBindPrompt.supplier_id == supplier_id)
    )
    await session.flush()


async def get_active_supplier_bind_prompt(
    session: AsyncSession,
    *,
    supplier_id: int,
) -> SupplierBindPrompt | None:
    prompt = await session.get(SupplierBindPrompt, supplier_id)
    if prompt is None:
        return None
    if prompt.expires_at < datetime.now(UTC):
        await clear_supplier_bind_prompt(session, supplier_id=supplier_id)
        return None
    return prompt


async def filter_unquoted_candidates(
    session: AsyncSession,
    *,
    supplier: Supplier,
    candidates: list[Request],
) -> list[Request]:
    if not candidates:
        return []
    quoted_ids = set(
        (
            await session.execute(
                select(Quote.request_id).where(
                    Quote.supplier_id == supplier.id,
                    Quote.request_id.in_([request.id for request in candidates]),
                )
            )
        ).scalars().all()
    )
    return [request for request in candidates if request.id not in quoted_ids]


async def start_supplier_bind_prompt(
    session: AsyncSession,
    *,
    supplier: Supplier,
    message_in: MessageIn,
    candidates: list[Request],
    raw_text: str,
    message_attrs: ProductAttrs,
    telegram: TelegramClientProtocol,
    business_connection_id: str | None,
) -> None:
    candidates = await filter_unquoted_candidates(
        session, supplier=supplier, candidates=candidates
    )
    ordered = rank_candidates_for_confirm(
        raw_text=raw_text,
        message_attrs=message_attrs,
        candidates=candidates,
    )
    if not ordered:
        return
    current = ordered[0]
    pending = [request.id for request in ordered[1:]]
    ttl = get_settings().supplier_bind_prompt_ttl_hours
    expires_at = datetime.now(UTC) + timedelta(hours=ttl)

    await clear_supplier_bind_prompt(session, supplier_id=supplier.id)
    session.add(
        SupplierBindPrompt(
            supplier_id=supplier.id,
            message_in_id=message_in.id,
            request_id=current.id,
            pending_request_ids=pending,
            chat_id=message_in.chat_id,
            business_connection_id=business_connection_id,
            expires_at=expires_at,
        )
    )
    await session.flush()
    if get_settings().supplier_bind_ask_enabled:
        await telegram.send_message(
            message_in.chat_id,
            render_template(
                "supplier_bind_confirm",
                request_text=request_preview_text(current),
            ),
            business_connection_id=business_connection_id,
        )
    logger.info(
        "supplier_bind_prompt started supplier_id={} message_in_id={} request_id={}",
        supplier.id,
        message_in.id,
        current.id,
    )


async def _advance_or_give_up(
    session: AsyncSession,
    *,
    prompt: SupplierBindPrompt,
    supplier: Supplier,
    telegram: TelegramClientProtocol,
    business_connection_id: str | None,
) -> bool:
    pending = list(prompt.pending_request_ids or [])
    if not pending:
        await clear_supplier_bind_prompt(session, supplier_id=supplier.id)
        if get_settings().supplier_bind_ask_enabled:
            await telegram.send_message(
                prompt.chat_id,
                render_template("supplier_bind_gave_up"),
                business_connection_id=business_connection_id,
            )
        return False
    next_id = int(pending.pop(0))
    prompt.request_id = next_id
    prompt.pending_request_ids = pending
    await session.flush()
    request = await session.get(Request, next_id)
    if request is None:
        return await _advance_or_give_up(
            session,
            prompt=prompt,
            supplier=supplier,
            telegram=telegram,
            business_connection_id=business_connection_id,
        )
    if get_settings().supplier_bind_ask_enabled:
        await telegram.send_message(
            prompt.chat_id,
            render_template(
                "supplier_bind_confirm",
                request_text=request_preview_text(request),
            ),
            business_connection_id=business_connection_id,
        )
    return True


async def try_handle_supplier_bind_prompt_reply(
    session: AsyncSession,
    *,
    supplier: Supplier,
    raw_text: str,
    chat_id: int,
    message_id: int,
    telegram: TelegramClientProtocol,
    business_connection_id: str | None,
) -> bool:
    """Handle yes/no while a bind prompt is active. Returns True if consumed."""
    prompt = await get_active_supplier_bind_prompt(session, supplier_id=supplier.id)
    if prompt is None:
        return False

    parsed = parse_supplier_reply(raw_text)
    if parsed.price is not None and not is_affirmative_reply(raw_text):
        await clear_supplier_bind_prompt(session, supplier_id=supplier.id)
        return False

    if is_negative_reply(raw_text):
        await _advance_or_give_up(
            session,
            prompt=prompt,
            supplier=supplier,
            telegram=telegram,
            business_connection_id=business_connection_id,
        )
        await _insert_prompt_ack_message(
            session,
            supplier_id=supplier.id,
            chat_id=chat_id,
            message_id=message_id,
            raw_text=raw_text,
            business_connection_id=business_connection_id,
            status="ignored",
        )
        return True

    if not is_affirmative_reply(raw_text):
        return False

    price_message = await session.get(MessageIn, prompt.message_in_id)
    request = await session.get(Request, prompt.request_id)
    if price_message is None or request is None:
        await clear_supplier_bind_prompt(session, supplier_id=supplier.id)
        return True

    await clear_supplier_bind_prompt(session, supplier_id=supplier.id)
    from app.handlers.supplier_messages import process_bound_supplier_reply

    await process_bound_supplier_reply(
        session,
        request=request,
        supplier=supplier,
        raw_text=price_message.raw_text,
        message_in=price_message,
        telegram=telegram,
        business_connection_id=prompt.business_connection_id,
        chat_id=prompt.chat_id,
        bind_method="confirm",
    )
    await _insert_prompt_ack_message(
        session,
        supplier_id=supplier.id,
        chat_id=chat_id,
        message_id=message_id,
        raw_text=raw_text,
        business_connection_id=business_connection_id,
        status="ignored",
    )
    return True


async def _insert_prompt_ack_message(
    session: AsyncSession,
    *,
    supplier_id: int,
    chat_id: int,
    message_id: int,
    raw_text: str,
    business_connection_id: str | None,
    status: str,
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
        bind_status=status,
        bind_score=None,
    )
