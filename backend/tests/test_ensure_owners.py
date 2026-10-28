"""Owner provisioning is idempotent and never prints secrets."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AdminAuditLog, Owner
from scripts.ensure_owners import provision_owners

OWNER_ID = 8226464648


@pytest.mark.asyncio
async def test_owner_created_once_then_noop(db_session: AsyncSession) -> None:
    first = await provision_owners(db_session, [OWNER_ID])
    assert first == {"created": 1, "existed": 0}

    owner = await db_session.scalar(select(Owner).where(Owner.telegram_id == OWNER_ID))
    assert owner is not None
    assert owner.dm_ok is False

    second = await provision_owners(db_session, [OWNER_ID])
    assert second == {"created": 0, "existed": 1}

    count = await db_session.scalar(
        select(func.count()).select_from(Owner).where(Owner.telegram_id == OWNER_ID)
    )
    assert count == 1

    provisioned = await db_session.scalar(
        select(func.count())
        .select_from(AdminAuditLog)
        .where(AdminAuditLog.action == "owner_provisioned")
    )
    existed = await db_session.scalar(
        select(func.count())
        .select_from(AdminAuditLog)
        .where(AdminAuditLog.action == "owner_already_existed")
    )
    assert provisioned == 1
    assert existed == 1
