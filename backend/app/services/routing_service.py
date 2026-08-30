"""Unified supplier/chat routing for RFQ, bargain, recheck, and inbound lookup.

This is the single source of truth for "where do we send messages to a supplier"
and "what supplier owns this chat".  No other module should read
``supplier.telegram_id`` directly for outbound routing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import ClientGroup, Supplier, SupplierChat, SupplierChatType

ChatRole = Literal["client_group", "supplier_chat", "unknown"]


@dataclass(frozen=True)
class RfqTarget:
    """Destination resolved for one supplier during an RFQ broadcast."""

    supplier: Supplier
    chat_id: int


async def chat_role(session: AsyncSession, chat_id: int) -> ChatRole:
    """Classify a chat id as client group, supplier chat, or unknown."""
    if await session.get(ClientGroup, {"chat_id": chat_id}) is not None:
        return "client_group"
    result = await session.execute(
        select(SupplierChat.id).where(
            SupplierChat.chat_id == chat_id,
            SupplierChat.active.is_(True),
        )
    )
    if result.scalar_one_or_none() is not None:
        return "supplier_chat"
    return "unknown"


async def resolve_rfq_targets(session: AsyncSession) -> list[RfqTarget]:
    """Return active suppliers that should receive an RFQ and their target chat.

    Conditions:
      * supplier.active = true
      * supplier.rfq_enabled = true
      * supplier has at least one active default chat (group or private)
    """
    result = await session.execute(
        select(Supplier)
        .where(
            Supplier.active.is_(True),
            Supplier.rfq_enabled.is_(True),
        )
        .options(selectinload(Supplier.chats))
    )
    suppliers = list(result.scalars().all())

    targets: list[RfqTarget] = []
    for supplier in suppliers:
        chat_id = _resolve_default_chat(supplier)
        if chat_id is not None:
            targets.append(RfqTarget(supplier=supplier, chat_id=chat_id))
    return targets


async def resolve_target_chat(session: AsyncSession, supplier_id: int) -> int | None:
    """Return the active default chat for a supplier, or fall back to private DM."""
    supplier = await session.get(Supplier, supplier_id)
    if supplier is None:
        return None
    return _resolve_default_chat(supplier)


async def resolve_supplier_by_chat(
    session: AsyncSession,
    chat_id: int,
) -> Supplier | None:
    """Return the supplier that owns this chat (group or private)."""
    result = await session.execute(
        select(Supplier)
        .join(SupplierChat, SupplierChat.supplier_id == Supplier.id)
        .where(
            SupplierChat.chat_id == chat_id,
            SupplierChat.active.is_(True),
        )
    )
    return result.scalar_one_or_none()


def _resolve_default_chat(supplier: Supplier) -> int | None:
    """Pick active default chat or fall back to the unique active private DM."""
    active_chats = [c for c in supplier.chats if c.active]

    defaults = [c for c in active_chats if c.is_default]
    if defaults:
        return defaults[0].chat_id

    private = [c for c in active_chats if c.chat_type == SupplierChatType.private]
    if len(private) == 1:
        return private[0].chat_id

    return None


async def is_client_group_active(session: AsyncSession, chat_id: int) -> bool:
    """Backward-compatible client group check.

    If no client groups are configured at all, any group is allowed (legacy
    behaviour). Once at least one group exists, only registered active groups
    are accepted.
    """
    from app.utils.whitelist import is_client_group_active as _legacy_check

    return await _legacy_check(session, chat_id)
