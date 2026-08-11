from __future__ import annotations

import pytest
from app.parsers.cache import build_content_hash, get_cached, set_cached
from sqlalchemy.ext.asyncio import AsyncSession


def test_build_content_hash_is_stable_for_whitespace() -> None:
    h1 = build_content_hash("supplier_reply", "  Цена  85000   руб ")
    h2 = build_content_hash("supplier_reply", "Цена 85000 руб")
    assert h1 == h2


@pytest.mark.asyncio
async def test_parse_cache_get_set_roundtrip(db_session: AsyncSession) -> None:
    kind = "supplier_reply"
    text = "Есть, 85000 руб"

    miss = await get_cached(db_session, kind=kind, raw_text=text)
    assert miss is None

    payload = {"price": 85000, "qty": 1, "confidence": 0.91}
    await set_cached(
        db_session,
        kind=kind,
        raw_text=text,
        result_json=payload,
        model_used="test-model",
    )

    hit = await get_cached(db_session, kind=kind, raw_text=" Есть,   85000 руб ")
    assert hit == payload
