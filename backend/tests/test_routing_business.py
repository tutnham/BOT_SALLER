"""routing_service business_dm priority and eligibility."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BusinessConnection, SupplierChat, SupplierChatType
from app.services.admin_service import add_supplier, set_supplier_default_chat
from app.services.app_settings_service import (
    PRIORITY_BUSINESS_DM_FIRST,
    PRIORITY_DEFAULT_CHAT_FIRST,
    ROUTING_BUSINESS_DM_PRIORITY_KEY,
    set_setting,
)
from app.services.routing_service import (
    chat_role,
    resolve_rfq_targets,
    resolve_supplier_by_chat,
    resolve_target,
    resolve_target_chat,
)


async def _seed(
    db_session: AsyncSession,
    *,
    telegram_id: int,
    connection_id: str,
    can_reply: bool = True,
    is_enabled: bool = True,
    chat_active: bool = True,
) -> tuple:
    supplier = await add_supplier(db_session, name="RouteBiz", telegram_id=telegram_id)
    db_session.add(
        BusinessConnection(
            tg_user_id=800000 + telegram_id % 1000,
            business_connection_id=connection_id,
            can_reply=can_reply,
            can_read_messages=True,
            is_enabled=is_enabled,
        )
    )
    biz = SupplierChat(
        supplier_id=supplier.id,
        chat_id=telegram_id,
        chat_type=SupplierChatType.business_dm,
        business_connection_id=connection_id,
        active=chat_active,
        is_default=False,
    )
    db_session.add(biz)
    await db_session.flush()
    return supplier, biz


@pytest.mark.asyncio
async def test_default_chat_first_prefers_group(
    db_session: AsyncSession,
) -> None:
    supplier, _ = await _seed(db_session, telegram_id=630001, connection_id="rt_def")
    group = SupplierChat(
        supplier_id=supplier.id,
        chat_id=-630001,
        chat_type=SupplierChatType.supergroup,
        is_default=False,
        active=True,
    )
    db_session.add(group)
    await db_session.flush()
    await set_supplier_default_chat(db_session, supplier.id, -630001)
    await set_setting(
        db_session, ROUTING_BUSINESS_DM_PRIORITY_KEY, PRIORITY_DEFAULT_CHAT_FIRST
    )

    target = await resolve_target(db_session, supplier.id)
    assert target is not None
    assert target.kind == "group"
    assert target.chat_id == -630001
    assert target.business_connection_id is None


@pytest.mark.asyncio
async def test_business_dm_first_prefers_business(
    db_session: AsyncSession,
) -> None:
    supplier, _ = await _seed(db_session, telegram_id=630002, connection_id="rt_biz")
    group = SupplierChat(
        supplier_id=supplier.id,
        chat_id=-630002,
        chat_type=SupplierChatType.supergroup,
        is_default=False,
        active=True,
    )
    db_session.add(group)
    await db_session.flush()
    await set_supplier_default_chat(db_session, supplier.id, -630002)
    await set_setting(
        db_session, ROUTING_BUSINESS_DM_PRIORITY_KEY, PRIORITY_BUSINESS_DM_FIRST
    )

    target = await resolve_target(db_session, supplier.id)
    assert target is not None
    assert target.kind == "business_dm"
    assert target.chat_id == 630002
    assert target.business_connection_id == "rt_biz"


@pytest.mark.asyncio
async def test_can_reply_false_not_selected(
    db_session: AsyncSession,
) -> None:
    supplier, _ = await _seed(
        db_session,
        telegram_id=630003,
        connection_id="rt_noreply",
        can_reply=False,
    )
    await set_setting(
        db_session, ROUTING_BUSINESS_DM_PRIORITY_KEY, PRIORITY_BUSINESS_DM_FIRST
    )
    target = await resolve_target(db_session, supplier.id)
    assert target is not None
    assert target.kind == "private"
    assert target.business_connection_id is None


@pytest.mark.asyncio
async def test_inactive_business_chat_not_selected(
    db_session: AsyncSession,
) -> None:
    supplier, _ = await _seed(
        db_session,
        telegram_id=630004,
        connection_id="rt_off",
        chat_active=False,
    )
    await set_setting(
        db_session, ROUTING_BUSINESS_DM_PRIORITY_KEY, PRIORITY_BUSINESS_DM_FIRST
    )
    target = await resolve_target(db_session, supplier.id)
    assert target is not None
    assert target.kind == "private"


@pytest.mark.asyncio
async def test_disabled_connection_not_selected(
    db_session: AsyncSession,
) -> None:
    supplier, _ = await _seed(
        db_session,
        telegram_id=630005,
        connection_id="rt_dis",
        is_enabled=False,
    )
    target = await resolve_target(db_session, supplier.id)
    assert target is not None
    assert target.kind != "business_dm"


@pytest.mark.asyncio
async def test_rfq_targets_include_business_only_supplier(
    db_session: AsyncSession,
) -> None:
    supplier = await add_supplier(db_session, name="OnlyBiz", telegram_id=630006)
    chats = (
        await db_session.execute(
            select(SupplierChat).where(SupplierChat.supplier_id == supplier.id)
        )
    ).scalars().all()
    private = next(c for c in chats if c.chat_type is SupplierChatType.private)
    private.active = False
    private.is_default = False
    db_session.add(
        BusinessConnection(
            tg_user_id=811006,
            business_connection_id="rt_only",
            can_reply=True,
            can_read_messages=True,
            is_enabled=True,
        )
    )
    db_session.add(
        SupplierChat(
            supplier_id=supplier.id,
            chat_id=630006,
            chat_type=SupplierChatType.business_dm,
            business_connection_id="rt_only",
            active=True,
            is_default=False,
        )
    )
    await db_session.flush()
    targets, _skipped = await resolve_rfq_targets(db_session)
    matched = [t for t in targets if t.supplier.id == supplier.id]
    assert len(matched) == 1
    assert matched[0].kind == "business_dm"
    assert matched[0].business_connection_id == "rt_only"


@pytest.mark.asyncio
async def test_chat_role_ignores_business_dm(
    db_session: AsyncSession,
) -> None:
    _supplier, _ = await _seed(db_session, telegram_id=630007, connection_id="rt_role")
    chats = (
        await db_session.execute(
            select(SupplierChat).where(SupplierChat.chat_id == 630007)
        )
    ).scalars().all()
    for chat in chats:
        if chat.chat_type is SupplierChatType.private:
            chat.active = False
    await db_session.flush()
    assert await chat_role(db_session, 630007) == "unknown"
    assert await resolve_supplier_by_chat(db_session, 630007) is None


@pytest.mark.asyncio
async def test_resolve_target_chat_wrapper(
    db_session: AsyncSession,
) -> None:
    supplier, _ = await _seed(db_session, telegram_id=630008, connection_id="rt_wrap")
    assert await resolve_target_chat(db_session, supplier.id) == 630008
