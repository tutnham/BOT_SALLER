"""Request creation and supplier broadcast (TECH DOC §9.1)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import MessageKind, Request, RequestStatus
from app.db.unit_of_work import commit_or_flush
from app.llm.client import (
    LLMProviderError,
    get_llm_client,
    validate_normalized_request_payload,
)
from app.parsers.cache import get_cached, set_cached
from app.parsers.request_normalizer import parse_request_text
from app.services.alert_service import notify_operators
from app.services.product_classifier import resolve_product_category, split_positions
from app.services.routing_service import resolve_rfq_targets
from app.services.supplier_delivery import deliver, is_sent
from app.telegram.client import TelegramClientProtocol, TelegramSendError
from app.templates.messages_ru import render_template

_NORMALIZE_KIND = "normalize_request"


@dataclass(frozen=True)
class CreateRequestOutcome:
    requests: list[Request]
    sent_count: int

    def __iter__(self) -> Any:
        """Backward-compatible unpack: primary request, sent_count."""
        primary = self.requests[0] if self.requests else None
        yield primary
        yield self.sent_count


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
        normalized = deterministic
    else:
        cached = await get_cached(session, kind=_NORMALIZE_KIND, raw_text=source_text)
        if cached is not None:
            parsed = validate_normalized_request_payload(cached)
            llm_payload = parsed.model_dump(mode="json")
            if parsed.confidence >= threshold and parsed.model.strip():
                normalized = _merge_normalized(deterministic, llm_payload)
            else:
                normalized = _build_fallback_normalized(source_text, deterministic)
        else:
            llm = get_llm_client()
            try:
                parsed = await llm.normalize_request(source_text)
            except LLMProviderError:
                normalized = _build_fallback_normalized(source_text, deterministic)
            else:
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
                    normalized = _merge_normalized(deterministic, llm_payload)
                else:
                    normalized = _build_fallback_normalized(source_text, deterministic)

    category = await resolve_product_category(session, source_text)
    normalized["category"] = category
    return normalized


async def _broadcast_request(
    session: AsyncSession,
    *,
    request: Request,
    source_text: str,
    category: str,
    telegram: TelegramClientProtocol,
) -> tuple[int, list[str]]:
    targets, skipped_no_categories = await resolve_rfq_targets(session, category=category)
    sent_count = 0
    failed_lines: list[str] = []

    if skipped_no_categories:
        names = ", ".join(f"{s.name} (#{s.id})" for s in skipped_no_categories)
        await notify_operators(
            session,
            render_template(
                "alert_suppliers_without_categories",
                names=names,
            ),
            telegram=telegram,
        )

    if category == "unknown":
        await notify_operators(
            session,
            render_template(
                "alert_unknown_product_category",
                request_id=request.id,
                source_text=source_text,
            ),
            telegram=telegram,
        )
        return 0, failed_lines

    if not targets:
        await notify_operators(
            session,
            render_template(
                "alert_no_suppliers_for_category",
                request_id=request.id,
                category=category,
                source_text=source_text,
            ),
            telegram=telegram,
        )
        try:
            await telegram.send_message(
                request.group_chat_id,
                render_template(
                    "ask_no_suppliers",
                    request_id=request.id,
                    category=category,
                ),
            )
        except TelegramSendError as exc:
            logger.warning(
                "Failed to notify group about missing suppliers request_id={}: {}",
                request.id,
                exc,
            )
        return 0, failed_lines

    for target in targets:
        supplier = target.supplier
        text = render_template(
            "ask",
            request_id=request.id,
            source_text=source_text,
        )
        outbound = await deliver(
            session,
            telegram,
            supplier_id=supplier.id,
            chat_id=target.chat_id,
            text=text,
            kind=MessageKind.ask,
            request_id=request.id,
            business_connection_id=target.business_connection_id,
        )
        if is_sent(outbound):
            sent_count += 1
            from app.application.supplier_rebind import retry_unbound_supplier_prices

            try:
                await retry_unbound_supplier_prices(
                    session, supplier=supplier, telegram=telegram
                )
            except Exception as exc:
                logger.warning(
                    "rebind_unbound failed supplier_id={} request_id={}: {}",
                    supplier.id,
                    request.id,
                    exc,
                )
        else:
            logger.warning(
                "Failed to send ask to supplier_id={} chat_id={}: {}",
                supplier.id,
                target.chat_id,
                outbound.error_text,
            )
            failed_lines.append(
                f"{supplier.name} (#{supplier.id}): {outbound.error_text or 'send_failed'}"
            )
    return sent_count, failed_lines


async def create_request(
    session: AsyncSession,
    *,
    group_chat_id: int,
    employee_id: int,
    source_text: str,
    telegram: TelegramClientProtocol,
) -> CreateRequestOutcome:
    """
    Create one or more requests (multi-position split), broadcast ``ask``.

    Commits before Telegram sends.
    """
    segments = split_positions(source_text)
    created: list[Request] = []
    total_sent = 0

    for segment in segments:
        normalized_json = await build_normalized_json(session, segment)
        category = str(normalized_json.get("category") or "unknown")
        request = Request(
            group_chat_id=group_chat_id,
            employee_id=employee_id,
            source_text=segment,
            normalized_json=normalized_json,
            status=RequestStatus.open,
        )
        session.add(request)
        await session.flush()
        created.append(request)

    await commit_or_flush(session)

    for request in created:
        category = str((request.normalized_json or {}).get("category") or "unknown")
        sent_count, failed_lines = await _broadcast_request(
            session,
            request=request,
            source_text=request.source_text,
            category=category,
            telegram=telegram,
        )
        total_sent += sent_count
        if sent_count >= 1:
            request.status = RequestStatus.awaiting_answers
        from app.services.request_status_service import load_request_rfq_counters

        counters = await load_request_rfq_counters(session, request.id)
        try:
            await telegram.send_message(
                request.group_chat_id,
                render_template(
                    "ask_status_card",
                    request_id=request.id,
                    sent_count=counters.sent_count,
                    replied_count=counters.replied_count,
                    quoted_count=counters.quoted_count,
                    pending_review_count=counters.pending_review_count,
                ),
            )
        except TelegramSendError:
            pass
        if failed_lines:
            try:
                await telegram.send_message(
                    request.group_chat_id,
                    render_template(
                        "supplier_delivery_failed",
                        request_id=request.id,
                        kind="ask",
                        failed_block="\n".join(failed_lines),
                    ),
                )
            except TelegramSendError as exc:
                logger.warning(
                    "Failed to notify group about partial ask delivery request_id={}: {}",
                    request.id,
                    exc,
                )

    await session.flush()
    return CreateRequestOutcome(requests=created, sent_count=total_sent)
