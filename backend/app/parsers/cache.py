"""Parse cache helpers (TECH DOC §8.5)."""

from __future__ import annotations

import hashlib
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ParseCache


def normalize_input_text(raw_text: str) -> str:
    """Normalize whitespace for stable cache keys."""
    return " ".join((raw_text or "").strip().split())


def build_content_hash(kind: str, raw_text: str) -> str:
    normalized = normalize_input_text(raw_text)
    payload = f"{kind}:{normalized}".encode()
    return hashlib.sha256(payload).hexdigest()


async def get_cached(
    session: AsyncSession,
    *,
    kind: str,
    raw_text: str,
) -> dict[str, Any] | None:
    content_hash = build_content_hash(kind, raw_text)
    result = await session.execute(
        select(ParseCache.result_json).where(
            ParseCache.kind == kind,
            ParseCache.content_hash == content_hash,
        )
    )
    cached = result.scalar_one_or_none()
    if isinstance(cached, dict):
        return cached
    return None


async def set_cached(
    session: AsyncSession,
    *,
    kind: str,
    raw_text: str,
    result_json: dict[str, Any],
    model_used: str | None,
) -> None:
    content_hash = build_content_hash(kind, raw_text)
    stmt = insert(ParseCache).values(
        content_hash=content_hash,
        kind=kind,
        result_json=result_json,
        model_used=model_used,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["content_hash"],
        set_={
            "kind": kind,
            "result_json": result_json,
            "model_used": model_used,
        },
    )
    await session.execute(stmt)
    await session.flush()
