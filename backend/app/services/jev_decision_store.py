"""Persistence for Jev decisions. Errors are not reused as cache hits."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import SupplierAiDecision
from app.integrations.jev.models import (
    JevDecisionAction,
    JevMessageClass,
    JevSupplierDecision,
)
from app.integrations.jev.prompts import PROMPT_VERSION, PROVIDER, SCHEMA_VERSION


def _row_to_decision(row: SupplierAiDecision) -> JevSupplierDecision:
    message_class = JevMessageClass(row.message_class or JevMessageClass.AMBIGUOUS.value)
    action = JevDecisionAction(row.recommended_action or JevDecisionAction.FALLBACK.value)
    return JevSupplierDecision(
        message_class=message_class,
        price_probability=_d(row.price_probability),
        selected_candidate_id=row.selected_request_id,
        selected_probability=_opt_d(row.selected_probability),
        second_probability=_opt_d(row.second_probability),
        confidence=_d(row.confidence),
        action=action,
        reason_code=row.reason_code or "jev_ambiguous",
        model=row.model,
        schema_version=row.schema_version,
        input_hash=row.input_hash,
        candidate_set_hash=row.candidate_set_hash,
        latency_ms=row.latency_ms,
        error_code=row.error_code,
        from_cache=True,
        prompt_version=row.prompt_version,
        provider_model=row.model,
    )


def _d(value: Decimal | None) -> Decimal:
    if value is None:
        return Decimal("0")
    return Decimal(str(value))


def _opt_d(value: Decimal | None) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


async def lookup_cached_decision(
    session: AsyncSession,
    *,
    input_hash: str,
    candidate_set_hash: str,
    model: str,
) -> JevSupplierDecision | None:
    settings = get_settings()
    cutoff = datetime.now(UTC) - timedelta(seconds=settings.jev_cache_ttl_seconds)
    row = await session.scalar(
        select(SupplierAiDecision)
        .where(
            SupplierAiDecision.provider == PROVIDER,
            SupplierAiDecision.input_hash == input_hash,
            SupplierAiDecision.candidate_set_hash == candidate_set_hash,
            SupplierAiDecision.model == model,
            SupplierAiDecision.prompt_version == PROMPT_VERSION,
            SupplierAiDecision.schema_version == SCHEMA_VERSION,
            SupplierAiDecision.error_code.is_(None),
            SupplierAiDecision.superseded_at.is_(None),
            SupplierAiDecision.created_at >= cutoff,
        )
        .order_by(SupplierAiDecision.id.desc())
        .limit(1)
    )
    if row is None or row.reason_code in {"route_mismatch", "supplier_inactive"}:
        return None
    return _row_to_decision(row)


async def persist_jev_decision(
    session: AsyncSession,
    *,
    decision: JevSupplierDecision,
    message_in_id: int,
    supplier_id: int,
    line_no: int,
    shadow_mode: bool,
    actual_action: str,
) -> SupplierAiDecision | None:
    if decision.error_code:
        row = SupplierAiDecision(
            message_in_id=message_in_id,
            supplier_id=supplier_id,
            line_no=line_no,
            provider=PROVIDER,
            model=decision.model or get_settings().jev_model,
            schema_version=SCHEMA_VERSION,
            prompt_version=PROMPT_VERSION,
            input_hash=decision.input_hash or "error",
            candidate_set_hash=decision.candidate_set_hash or "error",
            message_class=decision.message_class.value,
            price_probability=decision.price_probability,
            selected_request_id=decision.selected_candidate_id,
            selected_probability=decision.selected_probability,
            second_probability=decision.second_probability,
            confidence=decision.confidence,
            recommended_action=decision.action.value,
            actual_action=actual_action,
            reason_code=decision.reason_code,
            shadow_mode=shadow_mode,
            latency_ms=decision.latency_ms,
            error_code=decision.error_code,
            is_current=True,
        )
        session.add(row)
        await session.flush()
        return row

    existing = await session.scalar(
        select(SupplierAiDecision).where(
            SupplierAiDecision.message_in_id == message_in_id,
            SupplierAiDecision.line_no == line_no,
            SupplierAiDecision.input_hash == decision.input_hash,
            SupplierAiDecision.candidate_set_hash == decision.candidate_set_hash,
            SupplierAiDecision.model == decision.model,
            SupplierAiDecision.prompt_version == PROMPT_VERSION,
            SupplierAiDecision.schema_version == SCHEMA_VERSION,
            SupplierAiDecision.error_code.is_(None),
            SupplierAiDecision.superseded_at.is_(None),
        )
    )
    if existing is not None:
        existing.actual_action = actual_action
        await session.flush()
        return existing

    row = SupplierAiDecision(
        message_in_id=message_in_id,
        supplier_id=supplier_id,
        line_no=line_no,
        provider=PROVIDER,
        model=decision.model,
        schema_version=SCHEMA_VERSION,
        prompt_version=PROMPT_VERSION,
        input_hash=decision.input_hash,
        candidate_set_hash=decision.candidate_set_hash,
        message_class=decision.message_class.value,
        price_probability=decision.price_probability,
        selected_request_id=decision.selected_candidate_id,
        selected_probability=decision.selected_probability,
        second_probability=decision.second_probability,
        confidence=decision.confidence,
        recommended_action=decision.action.value,
        actual_action=actual_action,
        reason_code=decision.reason_code,
        shadow_mode=shadow_mode,
        latency_ms=decision.latency_ms,
        error_code=None,
        is_current=True,
    )
    session.add(row)
    await session.flush()
    return row


async def supersede_decisions_for_message(session: AsyncSession, message_in_id: int) -> None:
    rows = (
        await session.execute(
            select(SupplierAiDecision).where(
                SupplierAiDecision.message_in_id == message_in_id,
                SupplierAiDecision.superseded_at.is_(None),
            )
        )
    ).scalars().all()
    now = datetime.now(UTC)
    for row in rows:
        row.superseded_at = now
        row.is_current = False
    await session.flush()


async def record_employee_review(
    session: AsyncSession,
    *,
    message_in_id: int,
    outcome: str,
    employee_id: int | None,
) -> None:
    row = await session.scalar(
        select(SupplierAiDecision)
        .where(
            SupplierAiDecision.message_in_id == message_in_id,
            SupplierAiDecision.is_current.is_(True),
        )
        .order_by(SupplierAiDecision.id.desc())
        .limit(1)
    )
    if row is None:
        return
    row.review_outcome = outcome
    row.reviewed_at = datetime.now(UTC)
    row.reviewed_by = employee_id
    row.actual_action = outcome
    await session.flush()
