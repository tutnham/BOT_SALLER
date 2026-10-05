"""Let the worker own one transaction while sync handlers still commit."""

from __future__ import annotations

from contextvars import ContextVar

from sqlalchemy.ext.asyncio import AsyncSession

caller_owns_transaction: ContextVar[bool] = ContextVar(
    "caller_owns_transaction",
    default=False,
)


async def commit_or_flush(session: AsyncSession) -> None:
    """Commit unless the worker will commit the surrounding transaction."""
    if caller_owns_transaction.get():
        await session.flush()
    else:
        await session.commit()
