"""Typed request binding transitions."""

from __future__ import annotations

from app.db.models import Request, RequestStatus

TERMINAL_STATUSES = frozenset({RequestStatus.closed, RequestStatus.cancelled})

BIND_ALLOWED_STATUSES = frozenset(
    {
        RequestStatus.open,
        RequestStatus.awaiting_answers,
        RequestStatus.priced,
        RequestStatus.bargaining,
        RequestStatus.needs_recheck,
    }
)


class RequestNotBindableError(Exception):
    """Request cannot accept supplier binding."""


def assert_request_bindable(request: Request) -> None:
    if request.status in TERMINAL_STATUSES:
        raise RequestNotBindableError(request.id)


def can_auto_bind_request(request: Request) -> bool:
    return request.status in BIND_ALLOWED_STATUSES
