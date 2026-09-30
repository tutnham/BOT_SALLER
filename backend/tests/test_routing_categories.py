"""Supplier category routing for RFQ broadcast."""

from __future__ import annotations

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ProductCategory, SupplierCategory
from app.services.admin_service import add_supplier
from app.services.routing_service import resolve_rfq_targets


@pytest.mark.asyncio
async def test_samsung_not_sent_to_apple_only(db_session: AsyncSession) -> None:
    apple_only = await add_supplier(db_session, name="Apple Only", telegram_id=9101)
    await db_session.execute(
        delete(SupplierCategory).where(
            SupplierCategory.supplier_id == apple_only.id,
            SupplierCategory.category != ProductCategory.apple.value,
        )
    )
    await db_session.flush()

    targets, _ = await resolve_rfq_targets(db_session, category=ProductCategory.samsung.value)
    assert all(t.supplier.id != apple_only.id for t in targets)


@pytest.mark.asyncio
async def test_iphone_not_sent_to_samsung_only(db_session: AsyncSession) -> None:
    samsung_only = await add_supplier(db_session, name="Samsung Only", telegram_id=9102)
    await db_session.execute(
        delete(SupplierCategory).where(
            SupplierCategory.supplier_id == samsung_only.id,
            SupplierCategory.category != ProductCategory.samsung.value,
        )
    )
    await db_session.flush()

    targets, _ = await resolve_rfq_targets(db_session, category=ProductCategory.apple.value)
    assert all(t.supplier.id != samsung_only.id for t in targets)


@pytest.mark.asyncio
async def test_dual_category_supplier_gets_both(db_session: AsyncSession) -> None:
    dual = await add_supplier(db_session, name="Dual", telegram_id=9103)
    apple_targets, _ = await resolve_rfq_targets(db_session, category=ProductCategory.apple.value)
    samsung_targets, _ = await resolve_rfq_targets(
        db_session, category=ProductCategory.samsung.value
    )
    assert any(t.supplier.id == dual.id for t in apple_targets)
    assert any(t.supplier.id == dual.id for t in samsung_targets)


@pytest.mark.asyncio
async def test_supplier_without_categories_skipped(db_session: AsyncSession) -> None:
    bare = await add_supplier(db_session, name="Bare", telegram_id=9104)
    await db_session.execute(
        delete(SupplierCategory).where(SupplierCategory.supplier_id == bare.id)
    )
    await db_session.flush()

    targets, skipped = await resolve_rfq_targets(db_session, category=ProductCategory.apple.value)
    assert bare.id not in {t.supplier.id for t in targets}
    assert any(s.id == bare.id for s in skipped)
