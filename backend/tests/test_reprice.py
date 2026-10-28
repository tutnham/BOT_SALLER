"""Repricing replaces the published price and keeps history."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import (
    AdminAuditLog,
    QuoteSource,
    Request,
    RequestPriceSelection,
    RequestStatus,
    Supplier,
    TelegramOutbox,
)
from app.services.quote_service import upsert_quote
from app.services.reprice_service import confirm_reprice, reprice_dedupe_key


def _enable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTO_PUBLISH_FIRST_VALID_PRICE", "true")
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_reprice_creates_new_version_and_keeps_history(
    db_session: AsyncSession,
    seed_employee,
    seed_suppliers: list[Supplier],
    seed_markup_rules,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    request = Request(
        group_chat_id=-1001,
        employee_id=seed_employee.id,
        source_text="iPhone 17 Air 256GB Black",
        status=RequestStatus.awaiting_answers,
        requested_qty=1,
        normalized_json={"model": "17 Air", "qty": 1},
    )
    db_session.add(request)
    await db_session.flush()

    await upsert_quote(
        db_session, request.id, seed_suppliers[0].id,
        price=Decimal("95000"), source=QuoteSource.regex, confidence=0.9,
    )
    await upsert_quote(
        db_session, request.id, seed_suppliers[1].id,
        price=Decimal("90000"), source=QuoteSource.regex, confidence=0.9,
    )

    applied = await confirm_reprice(
        db_session,
        request_id=request.id,
        actor_employee_id=seed_employee.id,
        actor_telegram_id=seed_employee.telegram_id,
    )
    assert applied is True

    rows = (
        await db_session.scalars(
            select(RequestPriceSelection)
            .where(RequestPriceSelection.request_id == request.id)
            .order_by(RequestPriceSelection.selection_version.asc())
        )
    ).all()
    published = [row for row in rows if row.status == "published"]
    superseded = [row for row in rows if row.status == "superseded"]
    assert len(published) == 1
    assert len(superseded) == 1
    assert superseded[0].superseded_by_id == published[0].id
    assert published[0].publication_reason == "employee_reprice"
    assert published[0].client_unit_price < superseded[0].client_unit_price

    audit = await db_session.scalar(
        select(func.count()).select_from(AdminAuditLog).where(AdminAuditLog.action == "price_reprice")
    )
    assert audit == 1

    outbox_count = await db_session.scalar(
        select(func.count()).select_from(TelegramOutbox).where(
            TelegramOutbox.dedupe_key == reprice_dedupe_key(request.id, published[0].selection_version)
        )
    )
    assert outbox_count == 1


@pytest.mark.asyncio
async def test_reprice_without_better_price_is_noop(
    db_session: AsyncSession,
    seed_employee,
    seed_suppliers: list[Supplier],
    seed_markup_rules,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    request = Request(
        group_chat_id=-1001,
        employee_id=seed_employee.id,
        source_text="iPhone 17 Air 256GB Black",
        status=RequestStatus.awaiting_answers,
        requested_qty=1,
        normalized_json={"model": "17 Air", "qty": 1},
    )
    db_session.add(request)
    await db_session.flush()
    await upsert_quote(
        db_session, request.id, seed_suppliers[0].id,
        price=Decimal("91000"), source=QuoteSource.regex, confidence=0.9,
    )
    applied = await confirm_reprice(
        db_session,
        request_id=request.id,
        actor_employee_id=seed_employee.id,
        actor_telegram_id=seed_employee.telegram_id,
    )
    assert applied is False
