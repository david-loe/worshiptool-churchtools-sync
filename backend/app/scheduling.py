"""Pure, timezone-aware schedule calculations shared by API and scheduler."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from croniter import croniter


def next_schedule_after(
    *,
    schedule_type: str,
    interval_minutes: int | None,
    cron_expression: str | None,
    timezone_name: str,
    after: datetime,
) -> datetime:
    """Return the first scheduled UTC instant strictly after ``after``.

    Cron expressions are evaluated in the profile's target timezone.  The
    bounded retry protects against a malformed cron implementation result at a
    daylight-saving transition; validated schedules normally return on the
    first iteration.
    """

    base = _as_utc(after)
    if schedule_type == "interval":
        if interval_minutes is None or interval_minutes < 1:
            raise ValueError("interval schedule requires positive interval_minutes")
        return base + timedelta(minutes=interval_minutes)
    if schedule_type != "cron" or not cron_expression:
        raise ValueError("cron schedule requires cron_expression")
    zone = ZoneInfo(timezone_name)
    iterator = croniter(cron_expression, base.astimezone(zone))
    for _ in range(8):
        candidate = iterator.get_next(datetime)
        if candidate.tzinfo is None:
            candidate = candidate.replace(tzinfo=zone)
        candidate_utc = candidate.astimezone(timezone.utc)
        if candidate_utc > base:
            return candidate_utc
    raise ValueError("cron schedule did not produce a future instant")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
