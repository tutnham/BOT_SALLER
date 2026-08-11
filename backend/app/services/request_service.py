"""Request creation and supplier broadcast (TECH DOC §9.1)."""

from __future__ import annotations

from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import MessageKind, MessageOut, Request, RequestStatus, Supplier
from app.llm.client import (
    LLMProviderError,
    get_llm_client,
    validate_normalized_request_payload,
)
from app.parsers.cache import get_cached, set_cached
from app.parsers.request_normalizer import parse_request_text
from app.telegram.client import TelegramClientProtocol, TelegramSendError
from app.templates.messages_ru import render_template

_NORMALIZE_KIND = "normalize_request"


async def load_request_for_employee(
    session: AsyncSession,
    *,
    request_id: int,
    employee_id: int,
    chat_id: int,
    is_private_chat: bool,
) -> Request | None:
    """
    Load request only when employee is allowed to operate it.

    Group chat: request must belong to current group.
    Private chat: request must belong to same employee.
    """
    request = await session.get(Request, request_id)
    if request is None:
        return None

    if is_private_chat:
        return request if request.employee_id == employee_id else None
    return request if request.group_chat_id == chat_id else None


def _merge_normalized(
    deterministic: dict[str, Any],
    llm_payload: dict[str, Any],
) -> dict[str, Any]:
    merged = dict(deterministic)
    for field in ("model", "storage", "color", "region", "sim", "qty", "condition"):
        llm_value = llm_payload.get(field)
        if llm_value is not None and llm_value != "":
            merged[field] = llm_value
    merged["confidence"] = llm_payload.get("confidence", 0.0)
    return merged


def _build_fallback_normalized(
    source_text: str,
    deterministic: dict[str, Any],
) -> dict[str, Any]:
    return {
        "model": deterministic.get("model") or source_text,
        "storage": deterministic.get("storage"),
        "color": deterministic.get("color"),
        "region": deterministic.get("region"),
        "sim": deterministic.get("sim"),
        "qty": deterministic.get("qty"),
        "condition": deterministic.get("condition"),
        "confidence": deterministic.get("confidence", 0.0),
    }


async def build_normalized_json(
    session: AsyncSession,
    source_text: str,
) -> dict[str, Any]:
    """
    Normalize employee request text: regex first, LLM second, confidence gate always.
    """
    deterministic = parse_request_text(source_text)
    settings = get_settings()
    threshold = settings.confidence_threshold

    if deterministic.get("confidence", 0.0) >= threshold:
        return deterministic

    cached = await get_cached(session, kind=_NORMALIZE_KIND, raw_text=source_text)
    if cached is not None:
        parsed = validate_normalized_request_payload(cached)
        llm_payload = parsed.model_dump(mode="json")
        if parsed.confidence >= threshold and parsed.model.strip():
            return _merge_normalized(deterministic, llm_payload)
        return _build_fallback_normalized(source_text, deterministic)

    llm = get_llm_client()
    try:
        parsed = await llm.normalize_request(source_text)
    except LLMProviderError:
        return _build_fallback_normalized(source_text, deterministic)

    llm_payload = parsed.model_dump(mode="json")
    model_used = settings.llm_model or settings.llm_provider
    await set_cached(
        session,
        kind=_NORMALIZE_KIND,
        raw_text=source_text,
        result_json=llm_payload,
        model_used=model_used,
    )

    if parsed.confidence >= threshold and parsed.model.strip():
        return _merge_normalized(deterministic, llm_payload)
    return _build_fallback_normalized(source_text, deterministic)


async def get_eligible_suppliers(session: AsyncSession) -> list[Supplier]:
    """Suppliers that can receive Bot API DM (active + telegram_id + dm_ok)."""
    result = await session.execute(
        select(Supplier).where(
            Supplier.active.is_(True),
            Supplier.telegram_id.is_not(None),
            Supplier.dm_ok.is_(True),
        )
    )
    return list(result.scalars().all())


async def create_request(
    session: AsyncSession,
    *,
    group_chat_id: int,
    employee_id: int,
    source_text: str,
    telegram: TelegramClientProtocol,
) -> tuple[Request, int]:
    """
    Create request, broadcast ``ask`` to eligible suppliers, update status.

    Returns:
        (request, successful_send_count)
    """
    normalized_json = await build_normalized_json(session, source_text)
    request = Request(
        group_chat_id=group_chat_id,
        employee_id=employee_id,
        source_text=source_text,
        normalized_json=normalized_json,
        status=RequestStatus.open,
    )
    session.add(request)
    await session.flush()

    suppliers = await get_eligible_suppliers(session)
    sent_count = 0

    for supplier in suppliers:
        assert supplier.telegram_id is not None
        text = render_template(
            "ask",
            request_id=request.id,
            normalized_json=normalized_json,
        )
        try:
            message_id = await telegram.send_message(supplier.telegram_id, text)
        except TelegramSendError as exc:
            logger.warning(
                "Failed to send ask to supplier_id={}: {}",
                supplier.id,
                exc,
            )
            continue

        session.add(
            MessageOut(
                request_id=request.id,
                supplier_id=supplier.id,
                tg_message_id=message_id,
                chat_id=supplier.telegram_id,
                text=text,
                kind=MessageKind.ask,
            )
        )
        sent_count += 1

    if sent_count >= 1:
        request.status = RequestStatus.awaiting_answers

    await session.flush()
    return request, sent_count
