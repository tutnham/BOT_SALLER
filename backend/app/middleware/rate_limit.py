"""Simple in-memory rate limits for direct backend access."""

from __future__ import annotations

import os
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable
from time import monotonic

from fastapi import Request
from fastapi.responses import JSONResponse, Response

Bucket = deque[float]
MAX_BUCKETS = 4096


class InMemoryRateLimiter:
    def __init__(self) -> None:
        self._hits: OrderedDict[str, Bucket] = OrderedDict()

    def allow(self, key: str, *, limit: int, window_seconds: float) -> bool:
        now = monotonic()
        bucket = self._hits.get(key)
        if bucket is None:
            while len(self._hits) >= MAX_BUCKETS:
                self._hits.popitem(last=False)
            bucket = deque()
            self._hits[key] = bucket
        else:
            self._hits.move_to_end(key)
        threshold = now - window_seconds
        while bucket and bucket[0] < threshold:
            bucket.popleft()
        if len(bucket) >= limit:
            return False
        bucket.append(now)
        return True


_limiter = InMemoryRateLimiter()


async def rate_limit_webhook_and_jobs(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return await call_next(request)

    path = request.url.path
    if path.startswith("/telegram/webhook") or path.startswith("/jobs/"):
        body = await request.body()
        from_id = "anonymous"
        try:
            payload = await request.json()
            message = payload.get("message") if isinstance(payload, dict) else None
            from_user = message.get("from") if isinstance(message, dict) else None
            raw_from_id = from_user.get("id") if isinstance(from_user, dict) else None
            if raw_from_id is not None:
                from_id = str(raw_from_id)
        except Exception:
            from_id = "invalid-json"

        client_ip = request.client.host if request.client else "unknown"
        key_ip = f"ip:{client_ip}:{path}"
        key_user = f"user:{from_id}:{path}"

        if not _limiter.allow(key_ip, limit=120, window_seconds=60):
            return JSONResponse(status_code=429, content={"detail": "rate_limit_ip"})
        if not _limiter.allow(key_user, limit=60, window_seconds=60):
            return JSONResponse(status_code=429, content={"detail": "rate_limit_user"})

        request._body = body  # type: ignore[attr-defined]

    return await call_next(request)
