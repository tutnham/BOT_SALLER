"""Provision owners from BOOTSTRAP_OWNER_IDS. Idempotent, prints no secrets.

Usage: python -m scripts.ensure_owners [--telegram-id 8226464648]
"""

from __future__ import annotations

import argparse
import asyncio

from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.config import get_settings
from app.db.models import Owner
from app.db.session import get_session_factory
from app.services.audit_service import record_admin_mutation


def _parse_ids(raw: str) -> list[int]:
    ids: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if part:
            ids.append(int(part))
    return ids


async def provision_owners(session, telegram_ids: list[int]) -> dict[str, int]:
    created = 0
    existed = 0
    for telegram_id in telegram_ids:
        inserted = (
            await session.execute(
                pg_insert(Owner)
                .values(telegram_id=telegram_id, dm_ok=False)
                    .on_conflict_do_nothing(index_elements=["telegram_id"])
                .returning(Owner.id)
            )
        ).scalar_one_or_none()
        if inserted is None:
            existed += 1
            action = "owner_already_existed"
        else:
            created += 1
            action = "owner_provisioned"
        await record_admin_mutation(
            session,
            action=action,
            entity_type="owner",
            entity_id=str(telegram_id),
            new_state={"dm_ok": False},
        )
    return {"created": created, "existed": existed}


async def ensure_owners(telegram_ids: list[int]) -> dict[str, int]:
    factory = get_session_factory()
    async with factory() as session:
        result = await provision_owners(session, telegram_ids)
        await session.commit()
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Provision bootstrap owners")
    parser.add_argument("--telegram-id", type=int, action="append", default=[])
    args = parser.parse_args()

    ids = list(args.telegram_id)
    ids.extend(_parse_ids(get_settings().bootstrap_owner_ids))
    unique = list(dict.fromkeys(ids))
    if not unique:
        print("no owner ids provided")
        return
    result = asyncio.run(ensure_owners(unique))
    print(f"owners created={result['created']} existed={result['existed']}")


if __name__ == "__main__":
    main()
