"""Circuit breaker for Jev provider calls.

Opens after consecutive provider errors so a down vendor does not add
timeout latency to every supplier message. While open, classification
fails closed to the deterministic path (review/fallback), never to a
guess. Complements the ``JEV_ENABLED=false`` kill switch; it does not
replace it. State is per-process, like the rest of Jev telemetry.
"""

from __future__ import annotations

import time

from loguru import logger

from app.config import Settings

_STATE_CLOSED = "closed"
_STATE_OPEN = "open"
_STATE_HALF_OPEN = "half_open"

_state: str = _STATE_CLOSED
_failure_streak: int = 0
_opened_at: float | None = None
_probe_in_flight: bool = False


def reset_jev_breaker() -> None:
    """Test hook: back to closed with no history."""
    global _state, _failure_streak, _opened_at, _probe_in_flight
    _state = _STATE_CLOSED
    _failure_streak = 0
    _opened_at = None
    _probe_in_flight = False


def jev_breaker_snapshot() -> dict[str, object]:
    return {
        "state": _state,
        "failure_streak": _failure_streak,
        "opened_at_monotonic": _opened_at,
        "probe_in_flight": _probe_in_flight,
    }


def allow_jev_request(settings: Settings) -> bool:
    """Closed passes. Open waits for cooldown, then lets one probe through."""
    global _state, _probe_in_flight
    if _state == _STATE_CLOSED:
        return True
    if _state == _STATE_HALF_OPEN:
        # A probe is already running; everyone else fails closed.
        return False
    assert _opened_at is not None
    if time.monotonic() - _opened_at < settings.jev_breaker_cooldown_seconds:
        return False
    _state = _STATE_HALF_OPEN
    _probe_in_flight = True
    logger.info("jev_breaker half_open: probing provider after cooldown")
    return True


async def note_jev_breaker_success(settings: Settings) -> None:
    global _state, _failure_streak, _opened_at, _probe_in_flight
    was_degraded = _state != _STATE_CLOSED
    _state = _STATE_CLOSED
    _failure_streak = 0
    _opened_at = None
    _probe_in_flight = False
    if was_degraded:
        logger.info("jev_breaker closed: provider recovered")
        await _alert(
            "✅ Jev breaker closed: provider recovered, classification resumed.",
            trip=False,
        )


async def note_jev_breaker_failure(settings: Settings) -> None:
    global _state, _failure_streak, _opened_at, _probe_in_flight
    _probe_in_flight = False
    if _state == _STATE_HALF_OPEN:
        _state = _STATE_OPEN
        _opened_at = time.monotonic()
        logger.warning("jev_breaker re-opened: half-open probe failed")
        await _alert(
            "⚠️ Jev breaker re-opened: probe failed. Deterministic binding only.",
            trip=True,
        )
        return
    _failure_streak += 1
    if _state == _STATE_CLOSED and _failure_streak >= settings.jev_breaker_failure_streak:
        _state = _STATE_OPEN
        _opened_at = time.monotonic()
        logger.warning(
            "jev_breaker opened after {} consecutive provider errors", _failure_streak
        )
        await _alert(
            f"⚠️ Jev breaker opened after {_failure_streak} consecutive provider errors. "
            "Supplier messages fall back to deterministic binding until recovery.",
            trip=True,
        )


async def _alert(text: str, *, trip: bool) -> None:
    """Best-effort operator alert; never raises into the classification path.

    ``trip`` counts only open and re-open. Recovery is an alert, not a trip.
    """
    try:
        from app.services.alert_service import send_admin_alert
        from app.services.telemetry import record_jev_breaker_trip
        from app.telegram.client import get_telegram_client

        if trip:
            record_jev_breaker_trip()
        await send_admin_alert(text, telegram=get_telegram_client())
    except Exception:
        logger.exception("jev_breaker alert failed")