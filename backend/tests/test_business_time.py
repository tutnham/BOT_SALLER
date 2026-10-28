"""Business-day boundaries follow Europe/Moscow midnight, not UTC or server time."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.utils.business_time import get_business_day_bounds

TZ = "Europe/Moscow"


def _bounds(moment: datetime) -> tuple[datetime, datetime]:
    return get_business_day_bounds(moment, TZ)


def test_moscow_day_is_used_when_utc_date_differs() -> None:
    # 22:30 UTC is 01:30 next day in Moscow.
    moment = datetime(2026, 10, 28, 22, 30, tzinfo=UTC)
    start, end = _bounds(moment)
    assert start == datetime(2026, 10, 28, 21, 0, tzinfo=UTC)
    assert end == datetime(2026, 10, 29, 21, 0, tzinfo=UTC)


def test_exact_moscow_midnight_belongs_to_new_day() -> None:
    midnight = datetime(2026, 10, 28, 21, 0, tzinfo=UTC)
    start, end = _bounds(midnight)
    assert start == midnight
    assert end == midnight + timedelta(days=1)


def test_microsecond_before_midnight_belongs_to_previous_day() -> None:
    midnight = datetime(2026, 10, 28, 21, 0, tzinfo=UTC)
    moment = midnight - timedelta(microseconds=1)
    start, end = _bounds(moment)
    assert start == datetime(2026, 10, 27, 21, 0, tzinfo=UTC)
    assert end == midnight


def test_naive_datetime_is_treated_as_utc() -> None:
    naive = datetime(2026, 10, 28, 10, 0)
    aware = datetime(2026, 10, 28, 10, 0, tzinfo=UTC)
    assert _bounds(naive) == _bounds(aware)


def test_bounds_cover_exactly_one_calendar_day() -> None:
    moment = datetime(2026, 10, 28, 6, 0, tzinfo=UTC)
    start, end = _bounds(moment)
    assert end - start == timedelta(days=1)
