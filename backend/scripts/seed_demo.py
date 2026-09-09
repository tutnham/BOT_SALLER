"""Demo seed script for whitelist tables (owners, employees, suppliers, etc.)."""

from __future__ import annotations

import asyncio
import os

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings
from app.db.models import ClientGroup, Employee, MarkupRule, Owner, Supplier
from app.services.markup_service import seed_markup_rule_rows


def _env_int(name: str) -> int | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    return int(raw)


async def _upsert_owner(session: AsyncSession, telegram_id: int, name: str) -> None:
    stmt = insert(Owner).values(
        telegram_id=telegram_id,
        name=name,
        dm_ok=False,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["telegram_id"],
        set_={"name": name},
    )
    await session.execute(stmt)


async def _upsert_employee(session: AsyncSession, telegram_id: int, name: str) -> None:
    stmt = insert(Employee).values(
        telegram_id=telegram_id,
        name=name,
        active=True,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["telegram_id"],
        set_={"name": name, "active": True},
    )
    await session.execute(stmt)


async def _upsert_supplier(
    session: AsyncSession,
    *,
    telegram_id: int,
    name: str,
    price_channel_id: int | None = None,
) -> None:
    existing = await session.scalar(
        select(Supplier).where(Supplier.telegram_id == telegram_id)
    )
    if existing is None:
        session.add(
            Supplier(
                telegram_id=telegram_id,
                name=name,
                active=True,
                dm_ok=False,
                price_channel_id=price_channel_id,
            )
        )
    else:
        existing.name = name
        existing.active = True
        if price_channel_id is not None:
            existing.price_channel_id = price_channel_id


async def _upsert_client_group(
    session: AsyncSession,
    *,
    chat_id: int,
    title: str,
) -> None:
    stmt = insert(ClientGroup).values(
        chat_id=chat_id,
        title=title,
        active=True,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["chat_id"],
        set_={"title": title, "active": True},
    )
    await session.execute(stmt)


async def _ensure_markup_rules(session: AsyncSession) -> None:
    result = await session.execute(select(MarkupRule.id).limit(1))
    if result.scalar_one_or_none() is not None:
        return
    session.add_all(seed_markup_rule_rows())


async def seed_demo() -> None:
    settings = get_settings()
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    owner_id = _env_int("SEED_OWNER_TELEGRAM_ID")
    employee_id = _env_int("SEED_EMPLOYEE_TELEGRAM_ID")
    supplier_id = _env_int("SEED_SUPPLIER_TELEGRAM_ID")
    group_chat_id = _env_int("SEED_CLIENT_GROUP_CHAT_ID")
    supplier_channel_id = _env_int("SEED_SUPPLIER_PRICE_CHANNEL_ID")

    async with session_factory() as session:
        if owner_id is not None:
            await _upsert_owner(session, owner_id, os.environ.get("SEED_OWNER_NAME", "Owner"))
        if employee_id is not None:
            await _upsert_employee(
                session,
                employee_id,
                os.environ.get("SEED_EMPLOYEE_NAME", "Employee"),
            )
        if supplier_id is not None:
            await _upsert_supplier(
                session,
                telegram_id=supplier_id,
                name=os.environ.get("SEED_SUPPLIER_NAME", "Supplier"),
                price_channel_id=supplier_channel_id,
            )
        if group_chat_id is not None:
            await _upsert_client_group(
                session,
                chat_id=group_chat_id,
                title=os.environ.get("SEED_CLIENT_GROUP_TITLE", "Client Group"),
            )
        await _ensure_markup_rules(session)
        await session.commit()

    await engine.dispose()


def main() -> None:
    asyncio.run(seed_demo())


if __name__ == "__main__":
    main()
