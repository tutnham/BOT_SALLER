"""Reset parser tables between PostgreSQL integration tests.

Integration tests share one database. A leftover ``resolve_channel`` task
makes ``claim_next_task`` return a row the current test did not create.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def truncate_parser_tables(session: AsyncSession) -> None:
    await session.execute(text("TRUNCATE TABLE parser_channels RESTART IDENTITY CASCADE"))
    await session.commit()
