"""Business operations for the owner admin menu.

All write-side logic lives here; admin_menu.py handles only Telegram UI.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    AdminDialog,
    ClientGroup,
    PendingChat,
    Supplier,
    SupplierChat,
    SupplierChatType,
)
from app.services.routing_service import chat_role

_DIALOG_TTL = timedelta(minutes=15)


def _now() -> datetime:
    return datetime.now(UTC)


class ChatConflictError(Exception):
    """Chat is already bound to a different role."""


class UnknownPendingChatError(Exception):
    """Pending chat not found."""


class SupplierNotFoundError(Exception):
    """Supplier id not found."""


async def list_suppliers(session: AsyncSession) -> list[Supplier]:
    result = await session.execute(
        select(Supplier).order_by(Supplier.name.asc(), Supplier.id.asc())
    )
    return list(result.scalars().all())


async def list_supplier_chats(
    session: AsyncSession,
    supplier_id: int,
) -> list[SupplierChat]:
    result = await session.execute(
        select(SupplierChat)
        .where(SupplierChat.supplier_id == supplier_id)
        .order_by(SupplierChat.is_default.desc(), SupplierChat.created_at.desc())
    )
    return list(result.scalars().all())


async def add_supplier(
    session: AsyncSession,
    *,
    name: str,
    telegram_id: int | None = None,
    bound_by_owner_id: int | None = None,
) -> Supplier:
    supplier = Supplier(name=name, telegram_id=telegram_id, active=True, rfq_enabled=True)
    session.add(supplier)
    await session.flush()
    if telegram_id is not None:
        session.add(
            SupplierChat(
                supplier_id=supplier.id,
                chat_id=telegram_id,
                chat_type=SupplierChatType.private,
                is_default=True,
                active=True,
                bound_by_owner_id=bound_by_owner_id,
            )
        )
        await session.flush()
    return supplier


async def rename_supplier(
    session: AsyncSession,
    supplier_id: int,
    name: str,
) -> Supplier:
    supplier = await session.get(Supplier, supplier_id)
    if supplier is None:
        raise SupplierNotFoundError(supplier_id)
    supplier.name = name
    await session.flush()
    return supplier


async def toggle_supplier_active(
    session: AsyncSession,
    supplier_id: int,
) -> Supplier:
    supplier = await session.get(Supplier, supplier_id)
    if supplier is None:
        raise SupplierNotFoundError(supplier_id)
    supplier.active = not supplier.active
    await session.flush()
    return supplier


async def toggle_supplier_rfq(
    session: AsyncSession,
    supplier_id: int,
) -> Supplier:
    supplier = await session.get(Supplier, supplier_id)
    if supplier is None:
        raise SupplierNotFoundError(supplier_id)
    supplier.rfq_enabled = not supplier.rfq_enabled
    await session.flush()
    return supplier


async def set_supplier_default_chat(
    session: AsyncSession,
    supplier_id: int,
    chat_id: int,
) -> SupplierChat:
    result = await session.execute(
        select(SupplierChat).where(
            SupplierChat.supplier_id == supplier_id,
            SupplierChat.chat_id == chat_id,
        )
    )
    chat = result.scalar_one_or_none()
    if chat is None:
        raise SupplierNotFoundError(supplier_id)

    # Clear existing defaults for this supplier
    for c in await list_supplier_chats(session, supplier_id):
        if c.is_default:
            c.is_default = False
    chat.is_default = True
    chat.active = True
    await session.flush()
    return chat


async def toggle_chat_active(
    session: AsyncSession,
    supplier_id: int,
    chat_id: int,
) -> SupplierChat:
    result = await session.execute(
        select(SupplierChat).where(
            SupplierChat.supplier_id == supplier_id,
            SupplierChat.chat_id == chat_id,
        )
    )
    chat = result.scalar_one_or_none()
    if chat is None:
        raise SupplierNotFoundError(supplier_id)
    chat.active = not chat.active
    if not chat.active:
        chat.is_default = False
    await session.flush()
    return chat


async def bind_pending_chat_as_supplier(
    session: AsyncSession,
    *,
    chat_id: int,
    supplier_id: int,
    bound_by_owner_id: int,
) -> SupplierChat:
    pending = await session.get(PendingChat, chat_id)
    if pending is None:
        raise UnknownPendingChatError(chat_id)

    role = await chat_role(session, chat_id)
    if role in ("client_group", "supplier_chat"):
        raise ChatConflictError(role)

    supplier = await session.get(Supplier, supplier_id)
    if supplier is None:
        raise SupplierNotFoundError(supplier_id)

    chat_type = SupplierChatType(pending.chat_type.value)
    session.add(
        SupplierChat(
            supplier_id=supplier_id,
            chat_id=chat_id,
            chat_type=chat_type,
            title=pending.title,
            is_default=False,
            active=True,
            bound_by_owner_id=bound_by_owner_id,
        )
    )
    await session.delete(pending)
    await session.flush()
    return supplier


async def bind_pending_chat_as_client_group(
    session: AsyncSession,
    *,
    chat_id: int,
    title: str | None,
    bound_by_owner_id: int,
) -> ClientGroup:
    pending = await session.get(PendingChat, chat_id)
    if pending is None:
        raise UnknownPendingChatError(chat_id)

    role = await chat_role(session, chat_id)
    if role in ("client_group", "supplier_chat"):
        raise ChatConflictError(role)

    existing = await session.get(ClientGroup, {"chat_id": chat_id})
    if existing is not None:
        existing.active = True
        existing.title = title or existing.title or pending.title
        existing.bound_by_owner_id = bound_by_owner_id
    else:
        session.add(
            ClientGroup(
                chat_id=chat_id,
                title=title or pending.title,
                active=True,
                bound_by_owner_id=bound_by_owner_id,
            )
        )
    await session.delete(pending)
    await session.flush()
    return existing


async def list_client_groups(session: AsyncSession) -> list[ClientGroup]:
    result = await session.execute(
        select(ClientGroup).order_by(ClientGroup.title.asc(), ClientGroup.id.asc())
    )
    return list(result.scalars().all())


async def toggle_client_group(
    session: AsyncSession,
    group_id: int,
) -> ClientGroup:
    group = await session.get(ClientGroup, group_id)
    if group is None:
        raise SupplierNotFoundError(group_id)
    group.active = not group.active
    await session.flush()
    return group


async def set_dialog(
    session: AsyncSession,
    *,
    telegram_id: int,
    state: str,
    payload: dict[str, Any] | None = None,
) -> AdminDialog:
    dialog = await session.get(AdminDialog, telegram_id)
    if dialog is None:
        dialog = AdminDialog(telegram_id=telegram_id)
        session.add(dialog)
    dialog.state = state
    dialog.payload = payload or {}
    dialog.expires_at = _now() + _DIALOG_TTL
    await session.flush()
    return dialog


async def get_dialog(session: AsyncSession, telegram_id: int) -> AdminDialog | None:
    dialog = await session.get(AdminDialog, telegram_id)
    if dialog is None:
        return None
    if dialog.expires_at < _now():
        await session.delete(dialog)
        await session.flush()
        return None
    return dialog


async def clear_dialog(session: AsyncSession, telegram_id: int) -> None:
    dialog = await session.get(AdminDialog, telegram_id)
    if dialog is not None:
        await session.delete(dialog)
        await session.flush()


async def upsert_pending_chat(
    session: AsyncSession,
    *,
    chat_id: int,
    chat_type: str,
    title: str | None,
    invited_by_tg_id: int | None,
) -> PendingChat:
    pending = await session.get(PendingChat, chat_id)
    if pending is None:
        pending = PendingChat(chat_id=chat_id)
        session.add(pending)
    pending.chat_type = SupplierChatType(chat_type)
    pending.title = title or pending.title
    pending.invited_by_tg_id = invited_by_tg_id
    pending.created_at = _now()
    await session.flush()
    return pending


async def delete_pending_chat(session: AsyncSession, chat_id: int) -> None:
    pending = await session.get(PendingChat, chat_id)
    if pending is not None:
        await session.delete(pending)
        await session.flush()
