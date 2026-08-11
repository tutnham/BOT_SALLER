"""Telegram send throttling helpers."""

from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from time import monotonic


class TelegramThrottle:
    def __init__(self, *, global_rps: int = 25, per_chat_rps: int = 1) -> None:
        self._global_rps = global_rps
        self._per_chat_rps = per_chat_rps
        self._global_hits: deque[float] = deque()
        self._chat_hits: dict[int, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def wait(self, chat_id: int) -> None:
        while True:
            async with self._lock:
                now = monotonic()
                self._evict(self._global_hits, now)
                chat_bucket = self._chat_hits[chat_id]
                self._evict(chat_bucket, now)

                if len(self._global_hits) < self._global_rps and len(chat_bucket) < self._per_chat_rps:
                    self._global_hits.append(now)
                    chat_bucket.append(now)
                    return

                global_delay = self._delay(self._global_hits, now)
                chat_delay = self._delay(chat_bucket, now)
                delay = max(global_delay, chat_delay, 0.05)
            await asyncio.sleep(delay)

    @staticmethod
    def _evict(bucket: deque[float], now: float) -> None:
        edge = now - 1.0
        while bucket and bucket[0] < edge:
            bucket.popleft()

    @staticmethod
    def _delay(bucket: deque[float], now: float) -> float:
        if not bucket:
            return 0.0
        return max(0.0, 1.0 - (now - bucket[0]))


_throttle = TelegramThrottle()


async def throttle_send(chat_id: int) -> None:
    await _throttle.wait(chat_id)
