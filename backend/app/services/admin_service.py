"""Business operations for the owner admin menu.

All write-side logic lives here; admin_menu.py handles only Telegram UI.
"""

from __future__ import annotations

import enum
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    AdminDialog,
    ClientGroup,
    Employee,
    Owner,
    PendingChat,
    Supplier,
    SupplierBindToken,
    SupplierChat,
    SupplierChatType,
)
from app.services.routing_service import chat_role
from app.utils.whitelist import get_employee_by_telegram_id

_DIALOG_TTL = timedelta(minutes=15)


def _now() -> datetime:
    return datetime.now(UTC)


class ChatConflictError(Exception):
    """Chat is already bound to a different role."""


class UnknownPendingChatError(Exception):
    """Pending chat not found."""


class SupplierNotFoundError(Exception):
    """Supplier id not found."""


class EmployeeNotFoundError(Exception):
    """Employee id not found."""


class DuplicateEmployeeTelegramIdError(Exception):
    """Employee with this telegram_id already exists."""


class BindTokenResult(str, enum.Enum):
    """Outcome of consuming a supplier bind deep-link token."""

    ok = "ok"
    expired = "expired"
    used = "used"
    conflict = "conflict"


class PriceChannelBusyError(Exception):
    """Telegram price channel already bound to another supplier."""


ADMIN_LIST_LIMIT = 200


async def list_employees(session: AsyncSession) -> list[Employee]:
    result = await session.execute(
        select(Employee)
        .order_by(Employee.name.asc(), Employee.id.asc())
        .limit(ADMIN_LIST_LIMIT)
    )
    return list(result.scalars().all())


async def add_employee(
    session: AsyncSession,
    *,
    name: str,
    telegram_id: int,
) -> Employee:
    existing = await get_employee_by_telegram_id(
        session, telegram_id, require_active=False
    )
    if existing is not None:
        raise DuplicateEmployeeTelegramIdError(telegram_id)

    employee = Employee(name=name, telegram_id=telegram_id, active=True)
    session.add(employee)
    await session.flush()
    return employee


async def toggle_employee_active(
    session: AsyncSession,
    employee_id: int,
) -> Employee:
    employee = await session.get(Employee, employee_id)
    if employee is None:
        raise EmployeeNotFoundError(employee_id)
    employee.active = not employee.active
    await session.flush()
    return employee


async def list_suppliers(session: AsyncSession) -> list[Supplier]:
    result = await session.execute(
        select(Supplier)
        .order_by(Supplier.name.asc(), Supplier.id.asc())
        .limit(ADMIN_LIST_LIMIT)
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


async def _deactivate_old_private_chat(
    session: AsyncSession,
    supplier_id: int,
    old_tgid: int,
) -> None:
    """Mark the previous private chat as inactive/non-default when ID changes."""
    result = await session.execute(
        select(SupplierChat).where(
            SupplierChat.supplier_id == supplier_id,
            SupplierChat.chat_id == old_tgid,
            SupplierChat.chat_type == SupplierChatType.private,
        )
    )
    old_chat = result.scalar_one_or_none()
    if old_chat is not None:
        old_chat.active = False
        old_chat.is_default = False


async def _ensure_private_supplier_chat(
    session: AsyncSession,
    supplier: Supplier,
    telegram_id: int,
    bound_by_owner_id: int | None,
) -> None:
    """Create or reactivate a private SupplierChat for the given telegram_id."""
    chats = await list_supplier_chats(session, supplier.id)
    has_default = any(c.is_default and c.active for c in chats)

    existing = next(
        (c for c in chats if c.chat_id == telegram_id and c.chat_type == SupplierChatType.private),
        None,
    )
    if existing is not None:
        existing.active = True
        existing.is_default = True
    else:
        session.add(
            SupplierChat(
                supplier_id=supplier.id,
                chat_id=telegram_id,
                chat_type=SupplierChatType.private,
                is_default=not has_default,
                active=True,
                bound_by_owner_id=bound_by_owner_id,
            )
        )
        await session.flush()

    # Remove default flag from any other private chat of the same supplier.
    for c in chats:
        if c.chat_id != telegram_id and c.is_default and c.chat_type == SupplierChatType.private:
            c.is_default = False


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
        await _ensure_private_supplier_chat(
            session, supplier, telegram_id, bound_by_owner_id
        )
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
    chat = SupplierChat(
        supplier_id=supplier_id,
        chat_id=chat_id,
        chat_type=chat_type,
        title=pending.title,
        is_default=False,
        active=True,
        bound_by_owner_id=bound_by_owner_id,
    )
    session.add(chat)
    await session.delete(pending)
    await session.flush()
    return chat


class TelegramIdConflictError(Exception):
    """This Telegram ID is already used by owner, employee, or another supplier."""


_BIND_LINK_TTL = timedelta(hours=24)


async def _is_telegram_id_busy(
    session: AsyncSession,
    telegram_id: int,
    exclude_supplier_id: int | None = None,
) -> bool:
    """Check whether telegram_id is already bound to owner, employee, or another supplier."""
    owner = await session.execute(
        select(Owner).where(Owner.telegram_id == telegram_id).limit(1)
    )
    if owner.scalar_one_or_none() is not None:
        return True

    employee = await get_employee_by_telegram_id(
        session, telegram_id, require_active=False
    )
    if employee is not None:
        return True

    supplier_filters = [Supplier.telegram_id == telegram_id]
    if exclude_supplier_id is not None:
        supplier_filters.append(Supplier.id != exclude_supplier_id)
    supplier = await session.execute(
        select(Supplier).where(*supplier_filters).limit(1)
    )
    if supplier.scalar_one_or_none() is not None:
        return True

    return False


async def create_supplier_bind_token(
    session: AsyncSession,
    supplier_id: int,
    owner_id: int,
) -> SupplierBindToken:
    """Create a one-time deep-link token for supplier Telegram binding."""
    supplier = await session.get(Supplier, supplier_id)
    if supplier is None:
        raise SupplierNotFoundError(supplier_id)

    token_value = secrets.token_urlsafe(24)
    bind_token = SupplierBindToken(
        token=token_value,
        supplier_id=supplier_id,
        created_by_owner_id=owner_id,
        expires_at=_now() + _BIND_LINK_TTL,
    )
    session.add(bind_token)
    await session.flush()
    return bind_token


async def get_supplier_bind_token(
    session: AsyncSession,
    token: str,
) -> SupplierBindToken | None:
    """Fetch a bind token if it exists and has not been used or expired."""
    result = await session.execute(
        select(SupplierBindToken).where(SupplierBindToken.token == token).limit(1)
    )
    bind_token = result.scalar_one_or_none()
    if bind_token is None:
        return None
    if bind_token.used_at is not None:
        return bind_token
    if bind_token.expires_at < _now():
        return bind_token
    return bind_token


async def consume_supplier_bind_token(
    session: AsyncSession,
    token: str,
    telegram_id: int,
) -> tuple[BindTokenResult, SupplierBindToken | None]:
    """Validate and use a bind token, returning the outcome.

    On conflict the token is still burned so the owner can see the attempt
    and issue a new link.
    """
    result = await session.execute(
        select(SupplierBindToken).where(SupplierBindToken.token == token).limit(1)
    )
    bind_token = result.scalar_one_or_none()
    if bind_token is None:
        return BindTokenResult.expired, None

    if bind_token.used_at is not None:
        return BindTokenResult.used, bind_token

    if bind_token.expires_at < _now():
        return BindTokenResult.expired, bind_token

    supplier = await session.get(Supplier, bind_token.supplier_id)
    if supplier is None:
        bind_token.used_at = _now()
        return BindTokenResult.expired, bind_token

    if await _is_telegram_id_busy(session, telegram_id, exclude_supplier_id=supplier.id):
        bind_token.used_at = _now()
        return BindTokenResult.conflict, bind_token

    old_tgid = supplier.telegram_id
    supplier.telegram_id = telegram_id
    supplier.dm_ok = True
    await _ensure_private_supplier_chat(session, supplier, telegram_id, bind_token.created_by_owner_id)
    if old_tgid is not None and old_tgid != telegram_id:
        await _deactivate_old_private_chat(session, supplier.id, old_tgid)
    bind_token.used_at = _now()
    await session.flush()
    return BindTokenResult.ok, bind_token


async def bind_supplier_telegram_id(
    session: AsyncSession,
    *,
    supplier_id: int,
    telegram_id: int,
    bound_by_owner_id: int,
) -> Supplier:
    """Bind a Telegram user id to a supplier via owner forward/digits input."""
    supplier = await session.get(Supplier, supplier_id)
    if supplier is None:
        raise SupplierNotFoundError(supplier_id)

    if await _is_telegram_id_busy(session, telegram_id, exclude_supplier_id=supplier.id):
        raise TelegramIdConflictError(telegram_id)

    old_tgid = supplier.telegram_id
    supplier.telegram_id = telegram_id
    supplier.dm_ok = True
    await _ensure_private_supplier_chat(session, supplier, telegram_id, bound_by_owner_id)
    if old_tgid is not None and old_tgid != telegram_id:
        await _deactivate_old_private_chat(session, supplier.id, old_tgid)
    await session.flush()
    return supplier


async def unbind_supplier_telegram_id(
    session: AsyncSession,
    supplier_id: int,
) -> Supplier:
    """Remove private Telegram binding from a supplier; leave group chats untouched."""
    supplier = await session.get(Supplier, supplier_id)
    if supplier is None:
        raise SupplierNotFoundError(supplier_id)

    old_tgid = supplier.telegram_id
    supplier.telegram_id = None
    supplier.dm_ok = False

    if old_tgid is not None:
        await _deactivate_old_private_chat(session, supplier_id, old_tgid)

    await session.flush()
    return supplier


# Legacy aliases: old admin callbacks/tests use set_supplier_telegram_id /
# clear_supplier_telegram_id. New code should prefer bind_supplier_telegram_id /
# unbind_supplier_telegram_id.
set_supplier_telegram_id = bind_supplier_telegram_id
clear_supplier_telegram_id = unbind_supplier_telegram_id


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

    result = await session.execute(
        select(ClientGroup).where(ClientGroup.chat_id == chat_id)
    )
    existing = result.scalar_one_or_none()
    if existing is not None:
        existing.active = True
        existing.title = title or existing.title or pending.title
        existing.bound_by_owner_id = bound_by_owner_id
        group = existing
    else:
        group = ClientGroup(
            chat_id=chat_id,
            title=title or pending.title,
            active=True,
            bound_by_owner_id=bound_by_owner_id,
        )
        session.add(group)
    await session.delete(pending)
    await session.flush()
    return group


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


async def bind_supplier_price_channel(
    session: AsyncSession,
    *,
    supplier_id: int,
    channel_id: int,
    username: str | None,
) -> Supplier:
    supplier = await session.get(Supplier, supplier_id)
    if supplier is None:
        raise SupplierNotFoundError(supplier_id)

    result = await session.execute(
        select(Supplier).where(
            Supplier.price_channel_id == channel_id,
            Supplier.id != supplier_id,
        )
    )
    if result.scalar_one_or_none() is not None:
        raise PriceChannelBusyError(channel_id)

    supplier.price_channel_id = channel_id
    supplier.price_channel_username = username
    await session.flush()
    return supplier


async def unbind_supplier_price_channel(
    session: AsyncSession,
    supplier_id: int,
) -> Supplier:
    supplier = await session.get(Supplier, supplier_id)
    if supplier is None:
        raise SupplierNotFoundError(supplier_id)
    supplier.price_channel_id = None
    supplier.price_channel_username = None
    await session.flush()
    return supplier

