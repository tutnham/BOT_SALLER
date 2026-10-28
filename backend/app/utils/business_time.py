"""Moscow business-day boundaries. One definition of "today" for the whole app."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo


def get_business_day_bounds(
    now: datetime,
    timezone_name: str,
) -> tuple[datetime, datetime]:
    """Return [day_start, next_day_start) in UTC for the calendar day of ``now``.

    ``now`` is converted to ``timezone_name`` first, so the calendar day follows
    local midnight (Europe/Moscow by default), never the UTC or server date.
    """
    tz = ZoneInfo(timezone_name)
    moment = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
    local = moment.astimezone(tz)
    start_local = datetime.combine(local.date(), time.min, tzinfo=tz)
    next_local = start_local + timedelta(days=1)
    return start_local.astimezone(UTC), next_local.astimezone(UTC)
