"""Whitelist checks against owners / employees / suppliers (TECH DOC §5, §13)."""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Employee, Owner, Supplier


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


async def list_owner_telegram_ids(session: AsyncSession) -> list[int]:
    """Return all owner telegram ids for admin notifications."""
    result = await session.execute(select(Owner.telegram_id))
    return [int(row) for row in result.scalars().all()]


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


@dataclass(frozen=True)
class PriceApprover:
    """User allowed to approve or reject price drafts."""

    telegram_id: int
    employee_id: int | None


async def resolve_price_approver(
    session: AsyncSession,
    telegram_id: int,
) -> PriceApprover | None:
    """Active employee OR owner. None => no access."""
    employee = await get_employee_by_telegram_id(
        session, telegram_id, require_active=True
    )
    if employee is not None:
        return PriceApprover(telegram_id=telegram_id, employee_id=employee.id)
    if await is_owner(session, telegram_id):
        return PriceApprover(telegram_id=telegram_id, employee_id=None)
    return None


async def can_approve_price(session: AsyncSession, telegram_id: int) -> bool:
    return await resolve_price_approver(session, telegram_id) is not None
