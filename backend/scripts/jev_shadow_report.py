"""Shadow evaluation. Prints measured counts only. Never invents a rate."""

from __future__ import annotations

import asyncio
import json

from sqlalchemy import select

from app.db.models import SupplierAiDecision
from app.db.session import get_session_factory
from app.services.jev_shadow_eval import summarize


async def _main() -> None:
    factory = get_session_factory()
    async with factory() as session:
        rows = list(
            (
                await session.execute(
                    select(SupplierAiDecision).where(SupplierAiDecision.provider == "jev")
                )
            ).scalars().all()
        )
    print(json.dumps(summarize(rows), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(_main())
