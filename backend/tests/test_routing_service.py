"""Tests for routing_service."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ClientGroup, Supplier, SupplierChat, SupplierChatType
from app.services.admin_service import add_supplier
from app.services.routing_service import (
    chat_role,
    resolve_rfq_targets,
    resolve_supplier_by_chat,
    resolve_target_chat,
)


@pytest.mark.asyncio
async def test_resolve_rfq_targets_default_private_dm(db_session: AsyncSession) -> None:
    supplier = await add_supplier(db_session, name="Test Supplier", telegram_id=1000)
    targets = await resolve_rfq_targets(db_session)
    assert len(targets) == 1
    assert targets[0].supplier.id == supplier.id
    assert targets[0].chat_id == 1000


@pytest.mark.asyncio
async def test_resolve_rfq_targets_uses_default_group(db_session: AsyncSession) -> None:
    supplier = await add_supplier(db_session, name="Test Supplier", telegram_id=1000)
    group = SupplierChat(
        supplier_id=supplier.id,
        chat_id=-1001,
        chat_type=SupplierChatType.supergroup,
        title="Supplier Group",
        is_default=True,
        active=True,
    )
    db_session.add(group)
    await db_session.flush()

    targets = await resolve_rfq_targets(db_session)
    assert len(targets) == 1
    assert targets[0].chat_id == -1001


@pytest.mark.asyncio
async def test_resolve_rfq_targets_skip_inactive_and_disabled(db_session: AsyncSession) -> None:
    s1 = await add_supplier(db_session, name="Active", telegram_id=1001)
    s2 = await add_supplier(db_session, name="Inactive", telegram_id=1002)
    s2.active = False
    s3 = await add_supplier(db_session, name="No RFQ", telegram_id=1003)
    s3.rfq_enabled = False
    await db_session.flush()

    targets = await resolve_rfq_targets(db_session)
    assert {t.supplier.id for t in targets} == {s1.id}


@pytest.mark.asyncio
async def test_resolve_target_chat_private_fallback(db_session: AsyncSession) -> None:
    supplier = await add_supplier(db_session, name="Test Supplier", telegram_id=1000)
    assert await resolve_target_chat(db_session, supplier.id) == 1000

    chat = SupplierChat(
        supplier_id=supplier.id,
        chat_id=-1001,
        chat_type=SupplierChatType.supergroup,
        is_default=True,
        active=True,
    )
    db_session.add(chat)
    await db_session.flush()
    assert await resolve_target_chat(db_session, supplier.id) == -1001


@pytest.mark.asyncio
async def test_chat_role(db_session: AsyncSession) -> None:
    supplier = await add_supplier(db_session, name="Test Supplier", telegram_id=1000)
    chat = SupplierChat(
        supplier_id=supplier.id,
        chat_id=-1001,
        chat_type=SupplierChatType.supergroup,
        is_default=True,
        active=True,
    )
    db_session.add(chat)
    db_session.add(ClientGroup(chat_id=-2002, title="Client Group", active=True))
    await db_session.flush()

    assert await chat_role(db_session, -2002) == "client_group"
    assert await chat_role(db_session, -1001) == "supplier_chat"
    assert await chat_role(db_session, -9999) == "unknown"


@pytest.mark.asyncio
async def test_resolve_supplier_by_chat(db_session: AsyncSession) -> None:
    supplier = await add_supplier(db_session, name="Test Supplier", telegram_id=1000)
    chat = SupplierChat(
        supplier_id=supplier.id,
        chat_id=-1001,
        chat_type=SupplierChatType.supergroup,
        active=True,
    )
    db_session.add(chat)
    await db_session.flush()

    found = await resolve_supplier_by_chat(db_session, -1001)
    assert found is not None and found.id == supplier.id

    not_found = await resolve_supplier_by_chat(db_session, -9999)
    assert not_found is None
