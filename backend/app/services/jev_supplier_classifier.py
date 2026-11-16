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
from app.integrations.jev.prompts import BINDING_NONE, PROMPT_VERSION, SCHEMA_VERSION
from app.parsers.price_parse import is_bare_full_price_only, parse_price_from_text
from app.parsers.product_normalizer import (
    detect_product_contradictions,
    extract_product_attrs,
    model_identity_conflicts,
)
from app.services.candidate_loader_service import (
    JevOutboundFact,
    load_jev_outbound_facts,
)
from app.services.jev_breaker_service import (
    allow_jev_request,
    note_jev_breaker_failure,
    note_jev_breaker_success,
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
# TypeSafe starting band: <= no, >= yes, between is unsettled.
_MATCH_NO = Decimal("0.3")
_MATCH_YES = Decimal("0.7")


def _listed_match_band(value: Decimal | None) -> str:
    if value is None:
        return "unset"
    if value <= _MATCH_NO:
        return "no"
    if value >= _MATCH_YES:
        return "yes"
    return "mid"


def hash_message_input(message_text: str, *, settings: Settings | None = None) -> str:
    """Cache key includes live thresholds so an env change is visible immediately."""
    active = settings or get_settings()
    normalized = " ".join((message_text or "").strip().split())
    thresholds = (
        f"{active.jev_price_threshold}:{active.jev_bind_threshold}:{active.jev_bind_margin}"
    )
    payload = f"{PROMPT_VERSION}:{SCHEMA_VERSION}:{thresholds}:{normalized}".encode()
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
        model = _opt_str(normalized.get("model"))
        variant = _opt_str(normalized.get("variant"))
        # Variant is already inside display model ("17 Pro"); do not repeat it.
        variant_lead = ""
        if variant and variant.lower() not in {"base", "e"}:
            if model is None or variant.lower() not in model.lower():
                variant_lead = variant
        order_text = _order_text(request)
        attr_parts = [
            model or "",
            variant_lead,
            str(normalized.get("storage") or ""),
            str(normalized.get("color") or ""),
            str(normalized.get("sim") or ""),
            str(normalized.get("region") or ""),
        ]
        attrs = " ".join(part for part in attr_parts if part).strip()
        base = f"#{request.id} {order_text}".strip()
        # Always lead with identity. Skipping the prefix when attrs happen to
        # occur inside a long order text lets the 240-char cut drop the model.
        description = f"{attrs} | {base}" if attrs else base
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


def _as_cache_hit(
    cached: JevSupplierDecision,
    *,
    input_hash: str,
    candidate_set_hash: str,
) -> JevSupplierDecision:
    """Reuse the decision. Tokens and latency belong to the original provider call."""
    return cached.model_copy(
        update={
            "from_cache": True,
            "input_hash": input_hash,
            "candidate_set_hash": candidate_set_hash,
            "input_tokens": None,
            "output_tokens": None,
            "latency_ms": None,
        }
    )


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


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
        # binding_confidence is how peaked the candidate Choice is, not how sure
        # the price Noul is. Ignore only when the price probability itself is low.
        low_price = price_probability <= (Decimal("1") - settings.jev_price_threshold)
        if low_price:
            action = JevDecisionAction.IGNORE
            reason = "jev_not_price"
        else:
            reason = "jev_not_price"
    elif _listed_match_band(response.matches_listed_order) == "no":
        # Companion noul: nothing in the open orders is this model. Do not ask a person.
        action = JevDecisionAction.IGNORE
        reason = "jev_no_matching_request"
        selected_id = None
    elif message_class is JevMessageClass.AMBIGUOUS:
        selected_id = None
        reason = "jev_ambiguous"
    elif price_probability < settings.jev_price_threshold:
        reason = "jev_low_price_probability"
    elif (
        selected_key == BINDING_NONE
        and selected_probability is not None
        and selected_probability >= settings.jev_bind_threshold
    ):
        selected_id = None
        band = _listed_match_band(response.matches_listed_order)
        if band == "mid":
            reason = "jev_model_uncertain"
        elif band == "yes":
            reason = "jev_ambiguous"
        else:
            action = JevDecisionAction.IGNORE
            reason = "jev_no_matching_request"
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
            identity = model_identity_conflicts(attrs, request.normalized_json)
            conflicts = detect_product_contradictions(attrs, request.normalized_json)
            if identity:
                action = JevDecisionAction.IGNORE
                reason = "product_conflict"
            elif conflicts:
                reason = "product_conflict"
            elif _listed_match_band(response.matches_listed_order) == "mid":
                reason = "jev_model_uncertain"
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
        input_tokens=response.input_tokens,
        output_tokens=response.output_tokens,
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

    active_candidates = sorted(candidates, key=lambda item: item.id, reverse=True)[
        : settings.jev_max_candidates
    ]
    input_hash = hash_message_input(text, settings=settings)
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
        record_jev_decision(
            action=cached.action.value,
            shadow_mode=settings.jev_shadow_mode,
            reason_code=cached.reason_code,
        )
        return _as_cache_hit(
            cached, input_hash=input_hash, candidate_set_hash=candidate_hash
        )

    jev_candidates = candidates_to_jev(
        active_candidates,
        limit=settings.jev_max_candidates,
        outgoing=outgoing,
    )
    correlation_id = f"jev:{message_in_id or 0}:{line_no}"
    # Provider-only latency: DB prep and cache lookup above are not vendor time.
    provider_started = time.perf_counter()

    async def fail(
        code: str,
        *,
        latency_ms: int | None = None,
        provider_call: bool = True,
    ) -> JevSupplierDecision:
        return await _record_failure(
            code,
            provider_started,
            settings,
            session=session,
            message_in_id=message_in_id,
            supplier_id=supplier.id,
            line_no=line_no,
            input_hash=input_hash,
            candidate_set_hash=candidate_hash,
            latency_ms=latency_ms,
            provider_call=provider_call,
        )

    if not allow_jev_request(settings):
        return await fail("jev_breaker_open", provider_call=False)

    try:
        response = await get_jev_client().classify_supplier_message(
            message_text=text,
            candidates=jev_candidates,
            correlation_id=correlation_id,
        )
    except JevError as exc:
        latency_ms = _elapsed_ms(provider_started)
        await note_jev_breaker_failure(settings)
        return await fail(_error_code(exc), latency_ms=latency_ms)
    except Exception:
        logger.exception("jev classify failed without provider detail")
        latency_ms = _elapsed_ms(provider_started)
        await note_jev_breaker_failure(settings)
        return await fail("jev_unavailable", latency_ms=latency_ms)

    latency_ms = _elapsed_ms(provider_started)
    await note_jev_breaker_success(settings)
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
    record_jev_decision(
        action=decision.action.value,
        shadow_mode=settings.jev_shadow_mode,
        reason_code=decision.reason_code,
    )
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


def _error_code(exc: JevError) -> str:
    if isinstance(exc, JevTimeout):
        return "jev_timeout"
    if isinstance(exc, JevRateLimited):
        return "jev_rate_limited"
    if isinstance(exc, JevAuthError):
        return "jev_auth_error"
    if isinstance(exc, JevInvalidResponse):
        return "jev_invalid_response"
    return "jev_unavailable"


async def classify_lines_with_jev(
    session: AsyncSession,
    *,
    supplier: Supplier,
    business_connection_id: str | None,
    chat_id: int,
    message_in_id: int,
    lines: Sequence[tuple[int, str]],
    candidates: Sequence[Request],
    outgoing: Mapping[int, JevOutboundFact] | None = None,
    route_ok: bool = True,
) -> dict[int, JevSupplierDecision]:
    """Classify several lines of one supplier reply. Never raises to the webhook.

    Cache misses go to the provider as one fan-out request per chunk
    (shared candidate state, per-line questions). Adapters without the
    batch method fall back to sequential single-line calls: the shared
    AsyncSession is not safe for concurrent use, so parallelism lives in
    the provider, not in concurrent DB work.
    """
    settings = get_settings()
    results: dict[int, JevSupplierDecision] = {}
    valid: dict[int, str] = {}
    for line_no, raw in lines:
        text = (raw or "").strip()
        if not settings.jev_enabled:
            results[line_no] = _fallback(reason="jev_disabled")
        elif not text or len(text) > _MAX_TEXT:
            results[line_no] = _fallback(
                reason="jev_invalid_response", error_code="jev_invalid_response"
            )
        else:
            valid[line_no] = text
    if not valid:
        return results
    if not candidates:
        for line_no in valid:
            results[line_no] = _fallback(reason="jev_ambiguous")
        return results
    if not supplier.active:
        for line_no in valid:
            results[line_no] = _fallback(
                reason="supplier_inactive", action=JevDecisionAction.REVIEW
            )
        return results

    active_candidates = sorted(candidates, key=lambda item: item.id, reverse=True)[
        : settings.jev_max_candidates
    ]
    if outgoing is None:
        outgoing = await load_jev_outbound_facts(
            session,
            supplier_id=supplier.id,
            chat_id=chat_id,
            business_connection_id=business_connection_id,
            request_ids=[item.id for item in active_candidates],
        )
    candidate_hash = hash_candidate_set(active_candidates, outgoing)
    jev_candidates = candidates_to_jev(
        active_candidates,
        limit=settings.jev_max_candidates,
        outgoing=outgoing,
    )

    misses: dict[int, str] = {}
    for line_no, text in valid.items():
        input_hash = hash_message_input(text, settings=settings)
        cached = await lookup_cached_decision(
            session,
            input_hash=input_hash,
            candidate_set_hash=candidate_hash,
            model=settings.jev_model,
        )
        if cached is not None:
            record_jev_cache_hit()
            record_jev_decision(
                action=cached.action.value,
                shadow_mode=settings.jev_shadow_mode,
                reason_code=cached.reason_code,
            )
            results[line_no] = _as_cache_hit(
                cached, input_hash=input_hash, candidate_set_hash=candidate_hash
            )
        else:
            misses[line_no] = text
    if not misses:
        return results

    async def fail_line(
        line_no: int,
        code: str,
        started: float,
        *,
        latency_ms: int | None = None,
        provider_call: bool = True,
        count_error: bool = True,
    ) -> JevSupplierDecision:
        return await _record_failure(
            code,
            started,
            settings,
            session=session,
            message_in_id=message_in_id,
            supplier_id=supplier.id,
            line_no=line_no,
            input_hash=hash_message_input(misses[line_no], settings=settings),
            candidate_set_hash=candidate_hash,
            latency_ms=latency_ms,
            provider_call=provider_call,
            count_error=count_error,
        )

    client = get_jev_client()
    batch_fn = getattr(client, "classify_supplier_lines", None)
    # Legacy adapters own the breaker inside classify_with_jev. A pre-check
    # here would consume the half-open probe and leave the breaker stuck.
    if batch_fn is None:
        for line_no, text in misses.items():
            results[line_no] = await classify_with_jev(
                session,
                supplier=supplier,
                business_connection_id=business_connection_id,
                chat_id=chat_id,
                message_id=0,
                message_text=text,
                candidates=candidates,
                line_no=line_no,
                route_ok=route_ok,
                message_in_id=message_in_id,
                outgoing=outgoing,
            )
        return results

    chunk_size = max(1, settings.jev_batch_max_lines)
    miss_items = list(misses.items())
    for start in range(0, len(miss_items), chunk_size):
        chunk = dict(miss_items[start : start + chunk_size])
        if not allow_jev_request(settings):
            for line_no, _text in miss_items[start:]:
                results[line_no] = await fail_line(
                    line_no,
                    "jev_breaker_open",
                    time.perf_counter(),
                    provider_call=False,
                    count_error=False,
                )
            return results
        provider_started = time.perf_counter()
        try:
            answers = await batch_fn(
                lines=chunk,
                candidates=jev_candidates,
                correlation_id=f"jev:{message_in_id}:batch",
            )
        except JevError as exc:
            latency_ms = _elapsed_ms(provider_started)
            await note_jev_breaker_failure(settings)
            code = _error_code(exc)
            for index, line_no in enumerate(chunk):
                # One HTTP attempt: latency, request and error counters once.
                results[line_no] = await fail_line(
                    line_no,
                    code,
                    provider_started,
                    latency_ms=latency_ms,
                    provider_call=index == 0,
                    count_error=index == 0,
                )
            continue
        except Exception:
            logger.exception("jev batch classify failed without provider detail")
            latency_ms = _elapsed_ms(provider_started)
            await note_jev_breaker_failure(settings)
            for index, line_no in enumerate(chunk):
                results[line_no] = await fail_line(
                    line_no,
                    "jev_unavailable",
                    provider_started,
                    latency_ms=latency_ms,
                    provider_call=index == 0,
                    count_error=index == 0,
                )
            continue

        latency_ms = _elapsed_ms(provider_started)
        await note_jev_breaker_success(settings)
        record_jev_latency(latency_ms / 1000)
        note_jev_success()
        record_jev_request("ok")
        for line_no, text in chunk.items():
            answer = answers.get(line_no)
            if answer is None or isinstance(answer, JevInvalidResponse):
                # The chunk latency is already recorded once above.
                results[line_no] = await fail_line(
                    line_no,
                    "jev_invalid_response",
                    provider_started,
                    latency_ms=latency_ms,
                    provider_call=False,
                )
                continue
            decision = apply_provider_decision(
                response=answer,
                candidates=active_candidates,
                message_text=text,
                supplier_active=supplier.active,
                route_ok=route_ok,
                settings=settings,
            )
            decision = decision.model_copy(
                update={
                    "latency_ms": latency_ms,
                    "input_hash": hash_message_input(text, settings=settings),
                    "candidate_set_hash": candidate_hash,
                }
            )
            record_jev_decision(
                action=decision.action.value,
                shadow_mode=settings.jev_shadow_mode,
                reason_code=decision.reason_code,
            )
            if decision.action is JevDecisionAction.REVIEW:
                record_jev_review()
            if decision.action is JevDecisionAction.AUTO_BIND:
                record_jev_auto_bind_candidate()
            logger.info(
                "jev_decision correlation_id={} message_in_id={} provider={} model={} "
                "prompt_version={} schema_version={} action={} reason_code={} latency_ms={}",
                f"jev:{message_in_id}:{line_no}",
                message_in_id,
                "jev",
                decision.model,
                PROMPT_VERSION,
                SCHEMA_VERSION,
                decision.action.value,
                decision.reason_code,
                latency_ms,
            )
            await persist_jev_decision(
                session,
                decision=decision,
                message_in_id=message_in_id,
                supplier_id=supplier.id,
                line_no=line_no,
                shadow_mode=settings.jev_shadow_mode,
                actual_action="shadow" if settings.jev_shadow_mode else decision.action.value,
            )
            results[line_no] = decision
    return results


async def _record_failure(
    code: str,
    started: float,
    settings: Settings,
    *,
    session: AsyncSession | None = None,
    message_in_id: int | None = None,
    supplier_id: int | None = None,
    line_no: int = 1,
    input_hash: str | None = None,
    candidate_set_hash: str | None = None,
    latency_ms: int | None = None,
    provider_call: bool = True,
    count_error: bool = True,
) -> JevSupplierDecision:
    """Persist a fail-closed decision.

    ``provider_call`` is true only for the single observation of one HTTP
    attempt. Extra lines of the same chunk and breaker short-circuits must
    not add latency samples or request counts. ``count_error`` is false for
    ``jev_breaker_open``: that path never reached the provider.
    """
    measured = latency_ms if latency_ms is not None else _elapsed_ms(started)
    if count_error:
        record_jev_error(code)
    if provider_call:
        record_jev_latency(measured / 1000)
        record_jev_request("error")
    decision = _fallback(
        reason=code,
        error_code=code,
        model=settings.jev_model,
        latency_ms=measured if provider_call or latency_ms is not None else None,
    )
    decision = decision.model_copy(
        update={
            "input_hash": input_hash or "error",
            "candidate_set_hash": candidate_set_hash or "error",
        }
    )
    record_jev_decision(
        action=decision.action.value,
        shadow_mode=settings.jev_shadow_mode,
        reason_code=decision.reason_code,
    )
    logger.info(
        "jev_decision correlation_id={} message_in_id={} provider={} model={} "
        "prompt_version={} schema_version={} action={} reason_code={} latency_ms={}",
        "jev-error",
        message_in_id,
        "jev",
        settings.jev_model,
        PROMPT_VERSION,
        SCHEMA_VERSION,
        decision.action.value,
        decision.reason_code,
        decision.latency_ms,
    )
    if session is not None and message_in_id is not None and supplier_id is not None:
        await persist_jev_decision(
            session,
            decision=decision,
            message_in_id=message_in_id,
            supplier_id=supplier_id,
            line_no=line_no,
            shadow_mode=settings.jev_shadow_mode,
            actual_action="shadow" if settings.jev_shadow_mode else decision.action.value,
        )
    return decision
