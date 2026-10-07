"""Resolve supplier inbound message to an open request without strict Telegram reply."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import Request, Supplier
from app.llm.client import (
    LLMProviderError,
    get_llm_client,
    validate_supplier_reply_binding,
)
from app.parsers.cache import get_cached, set_cached
from app.parsers.product_normalizer import (
    ProductAttrs,
    attrs_match_score,
    extract_product_attrs,
    normalize_product_text,
)
from app.parsers.regex_parser import ParsedSupplierReply, parse_supplier_reply
from app.services.candidate_loader_service import load_eligible_candidates
from app.services.explicit_request_parser import parse_explicit_request_id
from app.services.route_identity import route_type_label
from app.services.telemetry import record_supplier_binding_decision

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
    candidates: list[Request] = field(default_factory=list)
    binding_price: Decimal | None = None
    needs_supplier_confirm: bool = False
    bind_session_id: int | None = None


def source_text_overlap_score(supplier_text: str, request_text: str) -> int:
    supplier_norm = normalize_product_text(supplier_text)
    request_norm = normalize_product_text(request_text)
    if not supplier_norm or not request_norm:
        return 0
    if supplier_norm in request_norm or request_norm in supplier_norm:
        return 4
    supplier_tokens = {token for token in supplier_norm.split() if len(token) >= 2}
    request_tokens = {token for token in request_norm.split() if len(token) >= 2}
    if not supplier_tokens or not request_tokens:
        return 0
    overlap = len(supplier_tokens & request_tokens)
    if overlap >= 3:
        return 3
    if overlap >= 2:
        return 2
    if overlap >= 1:
        return 1
    return 0


def _has_product_identity(attrs: ProductAttrs) -> bool:
    return bool(attrs.model or attrs.family or attrs.number)


def find_clear_text_match(
    raw_text: str,
    message_attrs: ProductAttrs,
    candidates: list[Request],
) -> Request | None:
    if not _has_product_identity(message_attrs):
        return None
    settings = get_settings()
    scored: list[tuple[Request, int]] = []
    for request in candidates:
        score = attrs_match_score(message_attrs, request.normalized_json)
        if score < 0:
            continue
        score += source_text_overlap_score(raw_text, request.source_text or "")
        scored.append((request, score))
    if not scored:
        return None
    scored.sort(key=lambda item: (item[1], -item[0].id), reverse=True)
    top_request, top_score = scored[0]
    second_score = scored[1][1] if len(scored) > 1 else -1
    margin = top_score - second_score
    if (
        top_score >= settings.supplier_bind_min_score
        and margin >= settings.supplier_bind_min_margin
    ):
        return top_request
    return None


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


async def _resolve_with_llm(
    session: AsyncSession,
    raw_text: str,
    candidates: list[Request],
) -> tuple[Request | None, float, Decimal | None]:
    payload_candidates = [
        {
            "id": request.id,
            "model": (request.normalized_json or {}).get("model"),
            "storage": (request.normalized_json or {}).get("storage"),
            "color": (request.normalized_json or {}).get("color"),
            "sim": (request.normalized_json or {}).get("sim"),
            "region": (request.normalized_json or {}).get("region"),
            "qty": (request.normalized_json or {}).get("qty"),
        }
        for request in candidates
    ]
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
            return None, 0.0, None
        settings = get_settings()
        await set_cached(
            session,
            kind=kind,
            raw_text=cache_key,
            result_json=binding.model_dump(mode="json"),
            model_used=settings.llm_model or settings.llm_provider,
        )

    threshold = get_settings().supplier_bind_llm_threshold
    if not binding.related or binding.confidence < threshold:
        from app.services.telemetry import record_supplier_binding_llm_abstain

        record_supplier_binding_llm_abstain()
        return None, binding.confidence, None
    if binding.request_id not in allowed_ids:
        return None, binding.confidence, None
    for request in candidates:
        if request.id == binding.request_id:
            return request, binding.confidence, binding.price
    return None, binding.confidence, None


def _log_binding(
    *,
    supplier: Supplier,
    method: str,
    request_id: int | None,
    score: float | None,
    candidate_count: int,
    business_connection_id: str | None,
    bind_confidence: float | None = None,
) -> None:
    logger.info(
        "supplier_bind supplier_id={} request_id={} method={} score={} "
        "candidate_count={} route_type={} bind_confidence={}",
        supplier.id,
        request_id,
        method,
        score,
        candidate_count,
        route_type_label(business_connection_id),
        bind_confidence,
    )
    record_supplier_binding_decision(method=method)


async def resolve_binding(
    session: AsyncSession,
    *,
    supplier: Supplier,
    message: dict[str, Any],
    raw_text: str,
    reply_request: Request | None,
    chat_id: int,
    business_connection_id: str | None,
    for_rebind: bool = False,
) -> BindingDecision:
    parsed = parse_supplier_reply(raw_text)
    message_attrs = extract_product_attrs(raw_text)

    if reply_request is not None:
        _log_binding(
            supplier=supplier,
            method="reply",
            request_id=reply_request.id,
            score=None,
            candidate_count=1,
            business_connection_id=business_connection_id,
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

    candidates = await load_eligible_candidates(
        session,
        supplier=supplier,
        chat_id=chat_id,
        business_connection_id=business_connection_id,
    )
    if not candidates:
        return BindingDecision(
            request=None,
            method="none",
            status="unbound",
            score=None,
            parsed=parsed,
        )

    explicit_id = parse_explicit_request_id(raw_text)
    if explicit_id is not None:
        for request in candidates:
            if request.id == explicit_id:
                _log_binding(
                    supplier=supplier,
                    method="explicit_request_id",
                    request_id=request.id,
                    score=None,
                    candidate_count=len(candidates),
                    business_connection_id=business_connection_id,
                )
                return BindingDecision(
                    request=request,
                    method="explicit_request_id",
                    status="bound",
                    score=None,
                    parsed=parsed,
                    candidates=candidates,
                )
        return BindingDecision(
            request=None,
            method="none",
            status="unbound",
            score=None,
            parsed=parsed,
            candidates=candidates,
        )

    if len(candidates) == 1:
        settings = get_settings()
        only = candidates[0]
        if settings.supplier_single_candidate_auto_bind_enabled or for_rebind:
            _log_binding(
                supplier=supplier,
                method="single_candidate",
                request_id=only.id,
                score=None,
                candidate_count=1,
                business_connection_id=business_connection_id,
            )
            return BindingDecision(
                request=only,
                method="single_candidate",
                status="bound",
                score=None,
                parsed=parsed,
                candidates=candidates,
            )
        logger.info(
            "supplier_bind_shadow single_candidate supplier_id={} would_request_id={}",
            supplier.id,
            only.id,
        )
        return BindingDecision(
            request=None,
            method="none",
            status="pending_binding",
            score=None,
            parsed=parsed,
            candidates=candidates,
            needs_supplier_confirm=True,
        )

    text_match = find_clear_text_match(raw_text, message_attrs, candidates)
    if text_match is not None:
        _log_binding(
            supplier=supplier,
            method="text",
            request_id=text_match.id,
            score=None,
            candidate_count=len(candidates),
            business_connection_id=business_connection_id,
        )
        return BindingDecision(
            request=text_match,
            method="text",
            status="bound",
            score=None,
            parsed=parsed,
            candidates=candidates,
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
            _log_binding(
                supplier=supplier,
                method="attrs",
                request_id=top_request.id,
                score=float(top_score),
                candidate_count=len(candidates),
                business_connection_id=business_connection_id,
            )
            return BindingDecision(
                request=top_request,
                method="attrs",
                status="bound",
                score=float(top_score),
                parsed=parsed,
                candidates=candidates,
            )

    if not _has_product_identity(message_attrs):
        return BindingDecision(
            request=None,
            method="none",
            status="pending_binding",
            score=None,
            parsed=parsed,
            candidates=candidates,
            needs_supplier_confirm=False,
        )

    llm_request, llm_conf, llm_price = await _resolve_with_llm(session, raw_text, candidates)
    if llm_request is not None:
        _log_binding(
            supplier=supplier,
            method="llm",
            request_id=llm_request.id,
            score=llm_conf,
            candidate_count=len(candidates),
            business_connection_id=business_connection_id,
            bind_confidence=llm_conf,
        )
        return BindingDecision(
            request=llm_request,
            method="llm",
            status="bound",
            score=llm_conf,
            parsed=merge_llm_price(parsed, llm_price),
            candidates=candidates,
            binding_price=llm_price,
        )

    return BindingDecision(
        request=None,
        method="none",
        status="pending_binding",
        score=None,
        parsed=parsed,
        candidates=candidates,
        needs_supplier_confirm=False,
    )


def merge_llm_price(parsed: ParsedSupplierReply, binding_price: Decimal | None) -> ParsedSupplierReply:
    if parsed.price is not None or binding_price is None:
        return parsed
    return parsed.model_copy(update={"price": binding_price, "confidence": max(parsed.confidence, 0.85)})
