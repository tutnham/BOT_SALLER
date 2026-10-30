"""Application-owned Jev decision policy. The provider does not choose AUTO_BIND."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping, Sequence
from decimal import Decimal

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.binding import can_auto_bind_request
from app.config import Settings, get_settings
from app.db.models import Request, Supplier
from app.services.candidate_loader_service import JevOutboundFact, load_jev_outbound_facts
from app.integrations.jev.client import get_jev_client
from app.integrations.jev.exceptions import (
    JevAuthError,
    JevError,
    JevInvalidResponse,
    JevRateLimited,
    JevTimeout,
)
from app.integrations.jev.models import (
    JevCandidate,
    JevDecisionAction,
    JevMessageClass,
    JevProviderResponse,
    JevSupplierDecision,
)
from app.integrations.jev.prompts import PROMPT_VERSION, SCHEMA_VERSION
from app.parsers.price_parse import is_bare_full_price_only, parse_price_from_text
from app.parsers.product_normalizer import (
    detect_product_contradictions,
    extract_product_attrs,
)
from app.services.jev_decision_store import (
    lookup_cached_decision,
    persist_jev_decision,
)
from app.services.telemetry import (
    note_jev_success,
    record_jev_auto_bind_candidate,
    record_jev_cache_hit,
    record_jev_decision,
    record_jev_error,
    record_jev_latency,
    record_jev_request,
    record_jev_review,
)

_MAX_TEXT = 4000


def hash_message_input(message_text: str) -> str:
    normalized = " ".join((message_text or "").strip().split())
    payload = f"{PROMPT_VERSION}:{SCHEMA_VERSION}:{normalized}".encode()
    return hashlib.sha256(payload).hexdigest()


def _resolve_qty(request: Request, normalized: Mapping[str, object]) -> int | None:
    for raw in (normalized.get("qty"), request.requested_qty):
        if isinstance(raw, bool) or raw is None:
            continue
        if isinstance(raw, int):
            return raw
        if isinstance(raw, str) and raw.strip().isdigit():
            return int(raw.strip())
    return None


def hash_candidate_set(
    candidates: Sequence[Request],
    outgoing: Mapping[int, JevOutboundFact] | None = None,
) -> str:
    rows: list[dict[str, object]] = []
    facts = outgoing or {}
    for request in sorted(candidates, key=lambda item: item.id):
        normalized = request.normalized_json or {}
        status = request.status.value if hasattr(request.status, "value") else str(request.status)
        fact = facts.get(request.id)
        rows.append(
            {
                "id": request.id,
                "status": status,
                "source_text": _order_text(request),
                "model": normalized.get("model"),
                "storage": normalized.get("storage"),
                "color": normalized.get("color"),
                "sim": normalized.get("sim"),
                "region": normalized.get("region"),
                "qty": _resolve_qty(request, normalized),
                "outgoing_at": fact.sent_at.isoformat() if fact is not None else None,
                "rfq_kind": fact.kind if fact is not None else None,
            }
        )
    raw = json.dumps(rows, sort_keys=True, default=str).encode()
    return hashlib.sha256(raw).hexdigest()


def _order_text(request: Request) -> str:
    return " ".join((request.source_text or "").split())[:400]


def candidates_to_jev(
    candidates: Sequence[Request],
    *,
    limit: int,
    outgoing: Mapping[int, JevOutboundFact] | None = None,
) -> list[JevCandidate]:
    selected = sorted(candidates, key=lambda item: item.id, reverse=True)[:limit]
    facts = outgoing or {}
    result: list[JevCandidate] = []
    for request in selected:
        normalized = request.normalized_json or {}
        model = normalized.get("model") or None
        order_text = _order_text(request)
        attr_parts = [
            str(model or ""),
            str(normalized.get("storage") or ""),
            str(normalized.get("color") or ""),
            str(normalized.get("sim") or ""),
            str(normalized.get("region") or ""),
        ]
        attrs = " ".join(part for part in attr_parts if part).strip()
        description = f"#{request.id} {order_text}".strip()
        if attrs and attrs not in description:
            description = f"{description} | {attrs}"
        description = description[:240] or f"request {request.id}"
        fact = facts.get(request.id)
        qty = _resolve_qty(request, normalized)
        result.append(
            JevCandidate(
                candidate_id=f"request:{request.id}",
                description=description,
                order_text=order_text or None,
                model=str(model) if model else None,
                storage=_opt_str(normalized.get("storage")),
                color=_opt_str(normalized.get("color")),
                sim=_opt_str(normalized.get("sim")),
                region=_opt_str(normalized.get("region")),
                quantity=qty,
                rfq_kind=fact.kind if fact is not None else "ask",
                outgoing_at=fact.sent_at.isoformat() if fact is not None else None,
            )
        )
    return result


def _opt_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _fallback(
    *,
    reason: str,
    error_code: str | None = None,
    model: str = "",
    latency_ms: int | None = None,
    action: JevDecisionAction = JevDecisionAction.FALLBACK,
) -> JevSupplierDecision:
    return JevSupplierDecision(
        message_class=JevMessageClass.AMBIGUOUS,
        price_probability=Decimal("0"),
        confidence=Decimal("0"),
        action=action,
        reason_code=reason,
        model=model,
        schema_version=SCHEMA_VERSION,
        prompt_version=PROMPT_VERSION,
        error_code=error_code,
        latency_ms=latency_ms,
    )


def apply_provider_decision(
    *,
    response: JevProviderResponse,
    candidates: Sequence[Request],
    message_text: str,
    supplier_active: bool,
    route_ok: bool,
    settings: Settings,
) -> JevSupplierDecision:
    """Thresholds, margin, and safety checks. Ignores any provider action."""
    try:
        message_class = JevMessageClass(response.message_choice)
    except ValueError:
        return _fallback(reason="jev_invalid_response", error_code="jev_invalid_response", model=response.model)

    probs = response.binding_probabilities
    selected_key = response.binding_choice
    selected_probability = probs.get(selected_key)
    others = [value for key, value in probs.items() if key != selected_key]
    second = max(others) if others else Decimal("0")
    selected_id = _parse_request_key(selected_key)
    allowed = {request.id: request for request in candidates}
    unknown = selected_id is not None and selected_id not in allowed
    if selected_key.startswith("request:") and selected_id is None:
        unknown = True

    price_probability = response.price_probability
    confidence = response.binding_confidence
    bare = is_bare_full_price_only(message_text, parse_price_from_text(message_text).price)
    bare_multi = bare and len(candidates) > 1

    action = JevDecisionAction.REVIEW
    reason = "jev_ambiguous"
    if not supplier_active:
        reason = "supplier_inactive"
    elif not route_ok:
        reason = "route_mismatch"
    elif unknown:
        reason = "jev_unknown_candidate"
        selected_id = None
    elif message_class is JevMessageClass.NOT_PRICE:
        selected_id = None
        low_price = price_probability <= (Decimal("1") - settings.jev_price_threshold)
        if low_price and confidence >= settings.jev_bind_threshold:
            action = JevDecisionAction.IGNORE
            reason = "jev_not_price"
        else:
            reason = "jev_not_price"
    elif message_class is JevMessageClass.AMBIGUOUS:
        selected_id = None
        reason = "jev_ambiguous"
    elif price_probability < settings.jev_price_threshold:
        reason = "jev_low_price_probability"
    elif selected_id is None or selected_probability is None:
        reason = "jev_ambiguous"
    elif selected_probability < settings.jev_bind_threshold:
        reason = "jev_low_binding_probability"
    elif (selected_probability - second) < settings.jev_bind_margin:
        reason = "jev_low_margin"
    elif bare_multi:
        reason = "multiple_candidates_bare_price"
        selected_id = None
    else:
        request = allowed.get(selected_id)
        if request is None or not can_auto_bind_request(request):
            reason = "request_not_bindable"
        else:
            attrs = extract_product_attrs(message_text)
            if detect_product_contradictions(attrs, request.normalized_json):
                reason = "product_conflict"
            else:
                action = JevDecisionAction.AUTO_BIND
                reason = "jev_price_and_binding_accepted"

    return JevSupplierDecision(
        message_class=message_class,
        price_probability=price_probability,
        selected_candidate_id=selected_id,
        selected_probability=selected_probability,
        second_probability=second,
        confidence=confidence,
        action=action,
        reason_code=reason,
        model=response.model,
        schema_version=SCHEMA_VERSION,
        prompt_version=PROMPT_VERSION,
        provider_model=response.model,
    )


def _parse_request_key(key: str) -> int | None:
    prefix = "request:"
    if not key.startswith(prefix):
        return None
    tail = key[len(prefix) :]
    if not tail.isdigit():
        return None
    return int(tail)


async def classify_with_jev(
    session: AsyncSession,
    *,
    supplier: Supplier,
    business_connection_id: str | None,
    chat_id: int,
    message_id: int,
    message_text: str,
    candidates: Sequence[Request],
    line_no: int = 1,
    route_ok: bool = True,
    message_in_id: int | None = None,
    outgoing: Mapping[int, JevOutboundFact] | None = None,
) -> JevSupplierDecision:
    """Classify one supplier message item. Never raises to the webhook."""
    del message_id
    settings = get_settings()
    if not settings.jev_enabled:
        return _fallback(reason="jev_disabled")
    text = (message_text or "").strip()
    if not text or len(text) > _MAX_TEXT:
        return _fallback(reason="jev_invalid_response", error_code="jev_invalid_response")
    if not candidates:
        return _fallback(reason="jev_ambiguous")
    if not supplier.active:
        return _fallback(reason="supplier_inactive", action=JevDecisionAction.REVIEW)

    started = time.perf_counter()
    active_candidates = sorted(candidates, key=lambda item: item.id, reverse=True)[
        : settings.jev_max_candidates
    ]
    input_hash = hash_message_input(text)
    if outgoing is None:
        outgoing = await load_jev_outbound_facts(
            session,
            supplier_id=supplier.id,
            chat_id=chat_id,
            business_connection_id=business_connection_id,
            request_ids=[item.id for item in active_candidates],
        )
    candidate_hash = hash_candidate_set(active_candidates, outgoing)
    cached = await lookup_cached_decision(
        session,
        input_hash=input_hash,
        candidate_set_hash=candidate_hash,
        model=settings.jev_model,
    )
    if cached is not None:
        record_jev_cache_hit()
        record_jev_decision(action=cached.action.value, shadow_mode=settings.jev_shadow_mode)
        return cached.model_copy(
            update={"from_cache": True, "input_hash": input_hash, "candidate_set_hash": candidate_hash}
        )

    jev_candidates = candidates_to_jev(
        active_candidates,
        limit=settings.jev_max_candidates,
        outgoing=outgoing,
    )
    correlation_id = f"jev:{message_in_id or 0}:{line_no}"
    try:
        response = await get_jev_client().classify_supplier_message(
            message_text=text,
            candidates=jev_candidates,
            correlation_id=correlation_id,
        )
    except JevTimeout:
        decision = _record_failure("jev_timeout", started, settings)
        return decision
    except JevRateLimited:
        return _record_failure("jev_rate_limited", started, settings)
    except JevAuthError:
        return _record_failure("jev_auth_error", started, settings)
    except JevInvalidResponse:
        return _record_failure("jev_invalid_response", started, settings)
    except JevError:
        return _record_failure("jev_unavailable", started, settings)
    except Exception:
        logger.exception("jev classify failed without provider detail")
        return _record_failure("jev_unavailable", started, settings)

    latency_ms = int((time.perf_counter() - started) * 1000)
    record_jev_latency(latency_ms / 1000)
    note_jev_success()
    record_jev_request("ok")
    decision = apply_provider_decision(
        response=response,
        candidates=active_candidates,
        message_text=text,
        supplier_active=supplier.active,
        route_ok=route_ok,
        settings=settings,
    )
    decision = decision.model_copy(
        update={
            "latency_ms": latency_ms,
            "input_hash": input_hash,
            "candidate_set_hash": candidate_hash,
        }
    )
    record_jev_decision(action=decision.action.value, shadow_mode=settings.jev_shadow_mode)
    if decision.action is JevDecisionAction.REVIEW:
        record_jev_review()
    if decision.action is JevDecisionAction.AUTO_BIND:
        record_jev_auto_bind_candidate()
    logger.info(
        "jev_decision correlation_id={} message_in_id={} provider={} model={} "
        "prompt_version={} schema_version={} action={} reason_code={} latency_ms={}",
        correlation_id,
        message_in_id,
        "jev",
        decision.model,
        PROMPT_VERSION,
        SCHEMA_VERSION,
        decision.action.value,
        decision.reason_code,
        latency_ms,
    )
    if message_in_id is not None:
        await persist_jev_decision(
            session,
            decision=decision,
            message_in_id=message_in_id,
            supplier_id=supplier.id,
            line_no=line_no,
            shadow_mode=settings.jev_shadow_mode,
            actual_action="shadow" if settings.jev_shadow_mode else decision.action.value,
        )
    return decision


def _record_failure(code: str, started: float, settings: Settings) -> JevSupplierDecision:
    latency_ms = int((time.perf_counter() - started) * 1000)
    record_jev_error(code)
    record_jev_latency(latency_ms / 1000)
    record_jev_request("error")
    decision = _fallback(
        reason=code,
        error_code=code,
        model=settings.jev_model,
        latency_ms=latency_ms,
    )
    record_jev_decision(action=decision.action.value, shadow_mode=settings.jev_shadow_mode)
    logger.info(
        "jev_decision correlation_id={} message_in_id={} provider={} model={} "
        "prompt_version={} schema_version={} action={} reason_code={} latency_ms={}",
        "jev-error",
        None,
        "jev",
        settings.jev_model,
        PROMPT_VERSION,
        SCHEMA_VERSION,
        decision.action.value,
        decision.reason_code,
        latency_ms,
    )
    return decision
