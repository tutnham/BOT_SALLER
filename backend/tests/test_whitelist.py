"""Whitelist helpers against owners / employees / suppliers."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Employee, Owner, Supplier
from app.utils.whitelist import is_employee, is_owner, is_supplier


@pytest.mark.asyncio
async def test_is_owner_happy_path(db_session: AsyncSession) -> None:
    db_session.add(Owner(telegram_id=1001, name="Owner"))
    await db_session.flush()

    assert await is_owner(db_session, 1001) is True
    assert await is_owner(db_session, 9999) is False


@pytest.mark.asyncio
async def test_is_employee_active_and_inactive(db_session: AsyncSession) -> None:
    db_session.add(Employee(telegram_id=2001, name="Active", active=True))
    db_session.add(Employee(telegram_id=2002, name="Inactive", active=False))
    await db_session.flush()

    assert await is_employee(db_session, 2001) is True
    assert await is_employee(db_session, 2002) is False
    assert await is_employee(db_session, 2002, require_active=False) is True
    assert await is_employee(db_session, 9999) is False


@pytest.mark.asyncio
async def test_is_supplier_happy_path(db_session: AsyncSession) -> None:
    db_session.add(Supplier(telegram_id=3001, name="Supplier A", active=True))
    db_session.add(Supplier(telegram_id=3002, name="Supplier B", active=False))
    await db_session.flush()

    assert await is_supplier(db_session, 3001) is True
    assert await is_supplier(db_session, 3002) is False
    assert await is_supplier(db_session, 3002, require_active=False) is True
    assert await is_supplier(db_session, 9999) is False
