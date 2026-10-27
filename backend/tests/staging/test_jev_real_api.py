"""Manual staging probe. Skipped unless JEV_STAGING=1 and JEV_API_KEY are set."""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.staging


@pytest.mark.asyncio
async def test_real_jev_smoke() -> None:
    if os.environ.get("JEV_STAGING") != "1" or not os.environ.get("JEV_API_KEY"):
        pytest.skip("set JEV_STAGING=1 and JEV_API_KEY to call the real API")
    from app.config import get_settings
    from app.integrations.jev.client import HttpJevClient
    from app.integrations.jev.models import JevCandidate

    get_settings.cache_clear()
    client = HttpJevClient()
    result = await client.classify_supplier_message(
        message_text="iPhone 17 Pro 256 Blue 117900",
        candidates=[JevCandidate(candidate_id="request:1", description="iPhone 17 Pro 256 Blue")],
        correlation_id="staging-smoke",
    )
    await client.aclose()
    assert result.model
    assert result.price_probability >= 0
