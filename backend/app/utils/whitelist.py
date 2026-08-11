"""Whitelist checks against owners / employees / suppliers (TECH DOC §5, §13)."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ClientGroup, Employee, Owner, Supplier


async def is_owner(session: AsyncSession, telegram_id: int) -> bool:
    """True if ``telegram_id`` exists in ``owners``."""
    result = await session.execute(
        select(Owner.id).where(Owner.telegram_id == telegram_id).limit(1)
    )
    return result.scalar_one_or_none() is not None


async def is_employee(
    session: AsyncSession,
    telegram_id: int,
    *,
    require_active: bool = True,
) -> bool:
    """True if ``telegram_id`` exists in ``employees`` (optionally ``active=true``)."""
    stmt = select(Employee.id).where(Employee.telegram_id == telegram_id)
    if require_active:
        stmt = stmt.where(Employee.active.is_(True))
    result = await session.execute(stmt.limit(1))
    return result.scalar_one_or_none() is not None


async def is_supplier(
    session: AsyncSession,
    telegram_id: int,
    *,
    require_active: bool = True,
) -> bool:
    """True if ``telegram_id`` exists in ``suppliers`` (optionally ``active=true``)."""
    stmt = select(Supplier.id).where(Supplier.telegram_id == telegram_id)
    if require_active:
        stmt = stmt.where(Supplier.active.is_(True))
    result = await session.execute(stmt.limit(1))
    return result.scalar_one_or_none() is not None


async def is_client_group_active(session: AsyncSession, chat_id: int) -> bool:
    """True when group chat is registered and active.

    Backward-compatible fallback: if no groups configured yet, allow all groups.
    """
    total = await session.execute(select(ClientGroup.id).limit(1))
    if total.scalar_one_or_none() is None:
        return True
    result = await session.execute(
        select(ClientGroup.id).where(
            ClientGroup.chat_id == chat_id,
            ClientGroup.active.is_(True),
        )
    )
    return result.scalar_one_or_none() is not None


async def get_employee_by_telegram_id(
    session: AsyncSession,
    telegram_id: int,
    *,
    require_active: bool = True,
) -> Employee | None:
    stmt = select(Employee).where(Employee.telegram_id == telegram_id)
    if require_active:
        stmt = stmt.where(Employee.active.is_(True))
    result = await session.execute(stmt.limit(1))
    return result.scalar_one_or_none()


async def get_owner_by_telegram_id(
    session: AsyncSession,
    telegram_id: int,
) -> Owner | None:
    result = await session.execute(
        select(Owner).where(Owner.telegram_id == telegram_id).limit(1)
    )
    return result.scalar_one_or_none()


async def get_supplier_by_telegram_id(
    session: AsyncSession,
    telegram_id: int,
    *,
    require_active: bool = True,
) -> Supplier | None:
    stmt = select(Supplier).where(Supplier.telegram_id == telegram_id)
    if require_active:
        stmt = stmt.where(Supplier.active.is_(True))
    result = await session.execute(stmt.limit(1))
    return result.scalar_one_or_none()
