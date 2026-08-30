"""FloodWait retry helper."""

from __future__ import annotations

import pytest

from app.mtproto.flood_wait import _flood_wait_seconds, with_flood_wait


class FloodWait(Exception):
    def __init__(self, value: int) -> None:
        self.value = value
        super().__init__(f"wait {value}")


@pytest.mark.asyncio
async def test_with_flood_wait_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("app.mtproto.flood_wait.asyncio.sleep", fake_sleep)

    calls = {"n": 0}

    async def flaky() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise FloodWait(2)
        return "ok"

    result = await with_flood_wait(flaky, max_retries=5)
    assert result == "ok"
    assert sleeps == [2, 2]


@pytest.mark.asyncio
async def test_non_flood_propagates() -> None:
    async def boom() -> None:
        raise ValueError("nope")

    with pytest.raises(ValueError, match="nope"):
        await with_flood_wait(boom, max_retries=2)


def test_flood_wait_seconds_x_attr() -> None:
    class FloodWaitX(Exception):
        def __init__(self) -> None:
            self.x = 7

    assert _flood_wait_seconds(FloodWaitX()) == 7
    assert _flood_wait_seconds(ValueError("x")) is None
