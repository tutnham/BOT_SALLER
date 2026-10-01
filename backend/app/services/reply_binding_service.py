"""Resolve supplier inbound message to an open request without strict Telegram reply."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import (
    MessageOut,
    MessageSendStatus,
    Quote,
    Request,
    RequestStatus,
    Supplier,
    SupplierCategory,
)
from app.llm.client import LLMProviderError, get_llm_client, validate_supplier_reply_binding
from app.parsers.cache import get_cached, set_cached
from app.parsers.product_normalizer import attrs_match_score, extract_product_attrs
from app.parsers.regex_parser import ParsedSupplierReply, parse_supplier_reply
from app.services.product_classifier import (
    category_from_normalized,
    classify_product_deterministic,
)


def _request_category(request: Request) -> str | None:
    category = category_from_normalized(request.normalized_json)
    if category:
        return category
    return classify_product_deterministic(request.source_text)

_ACTIVE_STATUSES = (
    RequestStatus.awaiting_answers,
    RequestStatus.bargaining,
    RequestStatus.needs_recheck,
    RequestStatus.priced,
    RequestStatus.open,
)

_NEUTRAL_RE = re.compile(
    r"^(?:"
    r"привет(?:ствую)?|здравствуйте|добрый\s+(?:день|вечер|утро)|"
    r"ok|ок|okay|спасибо|thanks|да|нет|\+|\-"
    r")[\s!.]*$",
    re.IGNORECASE,
)
_URL_RE = re.compile(r"https?://|t\.me/", re.IGNORECASE)
_PRICE_LINE_RE = re.compile(r"\d[\d\s]{2,}|\d+\s*(?:к|k|тыс|руб|₽)", re.IGNORECASE)


@dataclass(frozen=True)
class BindingDecision:
    request: Request | None
    method: str
    status: str
    score: float | None
    parsed: ParsedSupplierReply
    ignored_reason: str | None = None


def is_neutral_or_noise(raw_text: str) -> str | None:
    text = (raw_text or "").strip()
    if not text:
        return "empty"
    if _NEUTRAL_RE.match(text):
        return "neutral"
    if _URL_RE.search(text) and not _PRICE_LINE_RE.search(text):
        return "advertisement"
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    price_lines = sum(1 for line in lines if _PRICE_LINE_RE.search(line))
    if len(lines) >= 3 and price_lines >= 3:
        return "price_list"
    return None


async def load_open_requests_for_supplier(
    session: AsyncSession,
    *,
    supplier: Supplier,
) -> list[Request]:
    settings = get_settings()
    stmt = (
        select(Request)
        .join(
            MessageOut,
            MessageOut.request_id == Request.id,
        )
        .where(
            MessageOut.supplier_id == supplier.id,
            MessageOut.send_status == MessageSendStatus.sent.value,
            MessageOut.request_id.isnot(None),
            Request.status.in_(_ACTIVE_STATUSES),
        )
        .distinct()
        .order_by(Request.id.desc())
    )
    if settings.supplier_reply_max_age_hours is not None:
        cutoff = datetime.now(UTC) - timedelta(hours=settings.supplier_reply_max_age_hours)
        stmt = stmt.where(Request.updated_at >= cutoff)

    result = await session.execute(stmt)
    requests = list(result.scalars().all())

    cat_result = await session.execute(
        select(SupplierCategory.category).where(
            SupplierCategory.supplier_id == supplier.id
        )
    )
    supplier_categories = set(cat_result.scalars().all())
    if not supplier_categories:
        return []

    filtered: list[Request] = []
    for request in requests:
        category = _request_category(request)
        if category is None or category == "unknown":
            continue
        if category in supplier_categories:
            filtered.append(request)
    return filtered


def _candidate_payload(request: Request) -> dict[str, Any]:
    normalized = request.normalized_json or {}
    return {
        "id": request.id,
        "model": normalized.get("model"),
        "storage": normalized.get("storage"),
        "color": normalized.get("color"),
        "sim": normalized.get("sim"),
        "region": normalized.get("region"),
        "qty": normalized.get("qty"),
    }


async def _resolve_with_llm(
    session: AsyncSession,
    raw_text: str,
    candidates: list[Request],
) -> tuple[Request | None, float]:
    payload_candidates = [_candidate_payload(request) for request in candidates]
    allowed_ids = {request.id for request in candidates}
    cache_key = f"{raw_text}\n---\n" + "|".join(str(c["id"]) for c in payload_candidates)
    kind = "classify_supplier_reply"
    cached = await get_cached(session, kind=kind, raw_text=cache_key)
    if cached is not None:
        binding = validate_supplier_reply_binding(cached)
    else:
        llm = get_llm_client()
        try:
            binding = await llm.classify_supplier_reply(raw_text, payload_candidates)
        except LLMProviderError:
            return None, 0.0
        settings = get_settings()
        await set_cached(
            session,
            kind=kind,
            raw_text=cache_key,
            result_json=binding.model_dump(mode="json"),
            model_used=settings.llm_model or settings.llm_provider,
        )

    threshold = get_settings().confidence_threshold
    if not binding.related or binding.confidence < threshold:
        return None, binding.confidence
    if binding.request_id not in allowed_ids:
        return None, binding.confidence
    for request in candidates:
        if request.id == binding.request_id:
            return request, binding.confidence
    return None, binding.confidence


async def _oldest_unanswered(
    session: AsyncSession,
    supplier: Supplier,
    candidates: list[Request],
) -> Request | None:
    """First request sent to this supplier that still has no quote from them."""
    ids = [request.id for request in candidates]
    quoted = set(
        (
            await session.execute(
                select(Quote.request_id).where(
                    Quote.supplier_id == supplier.id,
                    Quote.request_id.in_(ids),
                )
            )
        ).scalars().all()
    )
    pending = [request for request in candidates if request.id not in quoted]
    if not pending:
        return None
    sent_at = dict(
        (
            await session.execute(
                select(MessageOut.request_id, func.min(MessageOut.sent_at))
                .where(
                    MessageOut.supplier_id == supplier.id,
                    MessageOut.request_id.in_([request.id for request in pending]),
                    MessageOut.send_status == MessageSendStatus.sent.value,
                )
                .group_by(MessageOut.request_id)
            )
        ).all()
    )
    pending.sort(key=lambda request: (sent_at.get(request.id) or request.created_at, request.id))
    return pending[0]


async def resolve_binding(
    session: AsyncSession,
    *,
    supplier: Supplier,
    message: dict[str, Any],
    raw_text: str,
    reply_request: Request | None,
) -> BindingDecision:
    parsed = parse_supplier_reply(raw_text)
    message_attrs = extract_product_attrs(raw_text)

    if reply_request is not None:
        logger.info(
            "supplier_bind message supplier_id={} request_id={} method=reply score=null",
            supplier.id,
            reply_request.id,
        )
        return BindingDecision(
            request=reply_request,
            method="reply",
            status="bound",
            score=None,
            parsed=parsed,
        )

    ignored = is_neutral_or_noise(raw_text)
    if ignored is not None:
        return BindingDecision(
            request=None,
            method="none",
            status="ignored",
            score=None,
            parsed=parsed,
            ignored_reason=ignored,
        )

    if parsed.price is None:
        return BindingDecision(
            request=None,
            method="none",
            status="ignored",
            score=None,
            parsed=parsed,
            ignored_reason="no_price",
        )

    candidates = await load_open_requests_for_supplier(session, supplier=supplier)
    if not candidates:
        return BindingDecision(
            request=None,
            method="none",
            status="unbound",
            score=None,
            parsed=parsed,
        )

    if len(candidates) == 1:
        logger.info(
            "supplier_bind message supplier_id={} request_id={} method=single score=null",
            supplier.id,
            candidates[0].id,
        )
        return BindingDecision(
            request=candidates[0],
            method="single",
            status="bound",
            score=None,
            parsed=parsed,
        )

    scored: list[tuple[Request, int]] = []
    for request in candidates:
        score = attrs_match_score(message_attrs, request.normalized_json)
        if score >= 0:
            scored.append((request, score))

    settings = get_settings()
    if scored:
        scored.sort(key=lambda item: item[1], reverse=True)
        top_request, top_score = scored[0]
        second_score = scored[1][1] if len(scored) > 1 else -1
        margin = top_score - second_score
        if (
            top_score >= settings.supplier_bind_min_score
            and margin >= settings.supplier_bind_min_margin
        ):
            logger.info(
                "supplier_bind message supplier_id={} request_id={} method=attrs score={}",
                supplier.id,
                top_request.id,
                top_score,
            )
            return BindingDecision(
                request=top_request,
                method="attrs",
                status="bound",
                score=float(top_score),
                parsed=parsed,
            )

    llm_request, llm_conf = await _resolve_with_llm(session, raw_text, candidates)
    if llm_request is not None:
        logger.info(
            "supplier_bind message supplier_id={} request_id={} method=llm score={}",
            supplier.id,
            llm_request.id,
            llm_conf,
        )
        return BindingDecision(
            request=llm_request,
            method="llm",
            status="bound",
            score=llm_conf,
            parsed=parsed,
        )

    if not message_attrs.model and not message_attrs.family and not message_attrs.number:
        ordered = await _oldest_unanswered(session, supplier, candidates)
        if ordered is not None:
            logger.info(
                "supplier_bind message supplier_id={} request_id={} method=order score=null",
                supplier.id,
                ordered.id,
            )
            return BindingDecision(
                request=ordered,
                method="order",
                status="bound",
                score=None,
                parsed=parsed,
            )

    return BindingDecision(
        request=None,
        method="none",
        status="unbound",
        score=None,
        parsed=parsed,
    )


def merge_llm_price(parsed: ParsedSupplierReply, binding_price: Decimal | None) -> ParsedSupplierReply:
    if parsed.price is not None or binding_price is None:
        return parsed
    return parsed.model_copy(update={"price": binding_price, "confidence": max(parsed.confidence, 0.85)})
