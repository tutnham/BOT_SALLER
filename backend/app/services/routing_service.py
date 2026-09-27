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

from app.db.models import (
    BusinessConnection,
    ClientGroup,
    Supplier,
    SupplierChat,
    SupplierChatType,
)
from app.services.app_settings_service import (
    PRIORITY_BUSINESS_DM_FIRST,
    PRIORITY_DEFAULT_CHAT_FIRST,
    ROUTING_BUSINESS_DM_PRIORITY_KEY,
    VALID_BUSINESS_DM_PRIORITIES,
    get_setting,
)

ChatRole = Literal["client_group", "supplier_chat", "unknown"]
RouteKind = Literal["business_dm", "group", "private"]


@dataclass(frozen=True)
class RouteTarget:
    """Resolved outbound destination for one supplier."""

    kind: RouteKind
    chat_id: int
    business_connection_id: str | None = None


@dataclass(frozen=True)
class RfqTarget:
    """Destination resolved for one supplier during an RFQ broadcast."""

    supplier: Supplier
    chat_id: int
    kind: RouteKind = "private"
    business_connection_id: str | None = None


async def chat_role(session: AsyncSession, chat_id: int) -> ChatRole:
    """Classify a chat id as client group, supplier chat, or unknown."""
    result = await session.execute(
        select(ClientGroup.id)
        .where(
            ClientGroup.chat_id == chat_id,
            ClientGroup.active.is_(True),
        )
        .limit(1)
    )
    if result.scalar_one_or_none() is not None:
        return "client_group"
    result = await session.execute(
        select(SupplierChat.id).where(
            SupplierChat.chat_id == chat_id,
            SupplierChat.active.is_(True),
            SupplierChat.chat_type != SupplierChatType.business_dm,
        )
    )
    if result.scalar_one_or_none() is not None:
        return "supplier_chat"
    return "unknown"


async def _load_priority(session: AsyncSession) -> str:
    raw = await get_setting(session, ROUTING_BUSINESS_DM_PRIORITY_KEY)
    if raw in VALID_BUSINESS_DM_PRIORITIES:
        return raw
    return PRIORITY_DEFAULT_CHAT_FIRST


async def _load_connections_by_id(
    session: AsyncSession,
    connection_ids: list[str],
) -> dict[str, BusinessConnection]:
    if not connection_ids:
        return {}
    result = await session.execute(
        select(BusinessConnection).where(
            BusinessConnection.business_connection_id.in_(connection_ids)
        )
    )
    return {row.business_connection_id: row for row in result.scalars().all()}


def _kind_from_chat(chat: SupplierChat) -> RouteKind:
    if chat.chat_type is SupplierChatType.business_dm:
        return "business_dm"
    if chat.chat_type in (SupplierChatType.group, SupplierChatType.supergroup):
        return "group"
    return "private"


def _as_business_target(
    chat: SupplierChat,
    connections: dict[str, BusinessConnection],
    *,
    supplier_telegram_id: int | None,
) -> RouteTarget | None:
    if supplier_telegram_id is None:
        return None
    if not chat.active or chat.chat_type is not SupplierChatType.business_dm:
        return None
    if chat.business_connection_id is None:
        return None
    connection = connections.get(chat.business_connection_id)
    if connection is None or not connection.is_enabled or not connection.can_reply:
        return None
    return RouteTarget(
        kind="business_dm",
        chat_id=chat.chat_id,
        business_connection_id=connection.business_connection_id,
    )


def _pick_target(
    supplier: Supplier,
    chats: list[SupplierChat],
    connections: dict[str, BusinessConnection],
    priority: str,
) -> RouteTarget | None:
    business: RouteTarget | None = None
    for chat in chats:
        candidate = _as_business_target(
            chat,
            connections,
            supplier_telegram_id=supplier.telegram_id,
        )
        if candidate is not None:
            business = candidate
            break

    default_target: RouteTarget | None = None
    defaults = [c for c in chats if c.active and c.is_default]
    if defaults:
        default_chat = defaults[0]
        if default_chat.chat_type is SupplierChatType.business_dm:
            default_target = _as_business_target(
                default_chat,
                connections,
                supplier_telegram_id=supplier.telegram_id,
            )
        else:
            default_target = RouteTarget(
                kind=_kind_from_chat(default_chat),
                chat_id=default_chat.chat_id,
            )

    if default_target is None:
        private = [
            c
            for c in chats
            if c.active and c.chat_type is SupplierChatType.private
        ]
        if len(private) == 1:
            default_target = RouteTarget(kind="private", chat_id=private[0].chat_id)

    if priority == PRIORITY_BUSINESS_DM_FIRST:
        return business or default_target
    return default_target or business


async def resolve_rfq_targets(session: AsyncSession) -> list[RfqTarget]:
    """Return active suppliers that should receive an RFQ and their target chat.

    Conditions:
      * supplier.active = true
      * supplier.rfq_enabled = true
      * supplier has a resolvable active route (default chat, private DM, or business_dm)
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
    connection_ids = [
        chat.business_connection_id
        for supplier in suppliers
        for chat in supplier.chats
        if chat.business_connection_id
    ]
    connections = await _load_connections_by_id(session, connection_ids)
    priority = await _load_priority(session)

    targets: list[RfqTarget] = []
    for supplier in suppliers:
        picked = _pick_target(supplier, list(supplier.chats), connections, priority)
        if picked is not None:
            targets.append(
                RfqTarget(
                    supplier=supplier,
                    chat_id=picked.chat_id,
                    kind=picked.kind,
                    business_connection_id=picked.business_connection_id,
                )
            )
    return targets


async def resolve_target(session: AsyncSession, supplier_id: int) -> RouteTarget | None:
    """Return the preferred outbound route for a supplier."""
    supplier = await session.get(Supplier, supplier_id)
    if supplier is None:
        return None
    result = await session.execute(
        select(SupplierChat).where(SupplierChat.supplier_id == supplier_id)
    )
    chats = list(result.scalars().all())
    connection_ids = [chat.business_connection_id for chat in chats if chat.business_connection_id]
    connections = await _load_connections_by_id(session, connection_ids)
    priority = await _load_priority(session)
    return _pick_target(supplier, chats, connections, priority)


async def resolve_target_chat(session: AsyncSession, supplier_id: int) -> int | None:
    """Return the active default chat for a supplier, or fall back to private DM."""
    target = await resolve_target(session, supplier_id)
    return target.chat_id if target is not None else None


async def resolve_supplier_by_chat(
    session: AsyncSession,
    chat_id: int,
) -> Supplier | None:
    """Return the supplier that owns this chat (group or private, not business_dm)."""
    result = await session.execute(
        select(Supplier)
        .join(SupplierChat, SupplierChat.supplier_id == Supplier.id)
        .where(
            SupplierChat.chat_id == chat_id,
            SupplierChat.active.is_(True),
            SupplierChat.chat_type != SupplierChatType.business_dm,
        )
    )
    return result.scalar_one_or_none()
