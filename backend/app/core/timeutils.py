"""Timezone-aware date/time primitives.

Rules enforced here (see docs/architecture/datetime.md):

* Everything persisted or compared is an aware UTC ``datetime``.
* Everything spoken to a caller is rendered in the *clinic's* timezone, which
  comes from the database (``clinics.timezone``), not from the server locale.
* Natural-language parsing lives in ``app.conversation.nlu.dates``; this module
  is pure arithmetic so it can be unit-tested without any NLU involvement.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo


class DayPeriod(StrEnum):
    """Coarse parts of the day a caller can ask for."""

    MORNING = "MORNING"
    AFTERNOON = "AFTERNOON"
    EVENING = "EVENING"
    ANY = "ANY"


# Local-time boundaries for each period. EVENING deliberately stops at 21:00 so
# a caller asking for "evening" is never offered a 23:30 slot.
PERIOD_BOUNDS: dict[DayPeriod, tuple[time, time]] = {
    DayPeriod.MORNING: (time(0, 0), time(12, 0)),
    DayPeriod.AFTERNOON: (time(12, 0), time(17, 0)),
    DayPeriod.EVENING: (time(17, 0), time(21, 0)),
    DayPeriod.ANY: (time(0, 0), time(23, 59, 59)),
}


def utc_now() -> datetime:
    """Current instant as an aware UTC datetime. The single source of 'now'."""
    return datetime.now(UTC)


def clinic_now(tz: ZoneInfo, *, now: datetime | None = None) -> datetime:
    """`now` rendered in the clinic timezone. Tests inject a fixed ``now``."""
    return (now or utc_now()).astimezone(tz)


def clinic_today(tz: ZoneInfo, *, now: datetime | None = None) -> date:
    return clinic_now(tz, now=now).date()


def to_utc(value: datetime) -> datetime:
    """Normalise to aware UTC. Naive input is rejected rather than guessed."""
    if value.tzinfo is None:
        raise ValueError("naive datetime is not permitted; attach a timezone first")
    return value.astimezone(UTC)


def local_datetime(day: date, at: time, tz: ZoneInfo) -> datetime:
    """Combine a clinic-local date and time into an aware UTC datetime.

    ``ZoneInfo`` resolves DST transitions deterministically (fold=0), which
    matters for Australian clinics crossing the October/April changeover.
    """
    return datetime.combine(day, at, tzinfo=tz).astimezone(UTC)


def day_bounds(day: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """[start, end) of a clinic-local calendar day, in UTC."""
    start = datetime.combine(day, time.min, tzinfo=tz)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz)
    return start.astimezone(UTC), end.astimezone(UTC)


def period_bounds(
    day: date,
    period: DayPeriod,
    tz: ZoneInfo,
    *,
    not_before_local: time | None = None,
) -> tuple[datetime, datetime]:
    """[start, end) of a period within a clinic-local day, in UTC.

    ``not_before_local`` narrows the window further; it backs phrasings such as
    "after 3" which combine with (or override) a period.
    """
    start_t, end_t = PERIOD_BOUNDS[period]
    if not_before_local is not None and not_before_local > start_t:
        start_t = not_before_local
    start = datetime.combine(day, start_t, tzinfo=tz)
    if end_t == time(23, 59, 59):
        end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz)
    else:
        end = datetime.combine(day, end_t, tzinfo=tz)
    return start.astimezone(UTC), max(start, end).astimezone(UTC)


def period_of(value: datetime, tz: ZoneInfo) -> DayPeriod:
    """Which period a given instant falls into, clinic-locally."""
    local_t = value.astimezone(tz).time()
    for period in (DayPeriod.MORNING, DayPeriod.AFTERNOON, DayPeriod.EVENING):
        start_t, end_t = PERIOD_BOUNDS[period]
        if start_t <= local_t < end_t:
            return period
    return DayPeriod.EVENING


# --- Speech-friendly rendering ---------------------------------------------
_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def speak_time(value: datetime, tz: ZoneInfo) -> str:
    """'2:30 pm' — no leading zero, lowercase meridiem, reads well aloud."""
    local = value.astimezone(tz)
    hour = local.hour % 12 or 12
    meridiem = "am" if local.hour < 12 else "pm"
    if local.minute == 0:
        return f"{hour} {meridiem}"
    return f"{hour}:{local.minute:02d} {meridiem}"


def speak_date(value: datetime | date, tz: ZoneInfo, *, today: date | None = None) -> str:
    """'today', 'tomorrow', 'Friday' (this week) or 'Friday the 21st'."""
    day = value.astimezone(tz).date() if isinstance(value, datetime) else value
    today = today or clinic_today(tz)
    delta = (day - today).days
    if delta == 0:
        return "today"
    if delta == 1:
        return "tomorrow"
    weekday = _WEEKDAYS[day.weekday()]
    if 0 < delta < 7:
        return weekday
    return f"{weekday} the {ordinal(day.day)}"


def speak_datetime(value: datetime, tz: ZoneInfo, *, today: date | None = None) -> str:
    return f"{speak_date(value, tz, today=today)} at {speak_time(value, tz)}"


def ordinal(n: int) -> str:
    """1 -> '1st'. Used when speaking dates ('the 21st')."""
    if 11 <= (n % 100) <= 13:
        return f"{n}th"
    return f"{n}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th') }".replace(" ", "")


@dataclass(frozen=True, slots=True)
class SearchWindow:
    """A resolved, validated window to search for availability in."""

    start: datetime
    end: datetime
    day: date | None = None
    period: DayPeriod = DayPeriod.ANY

    def __post_init__(self) -> None:
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("SearchWindow requires aware datetimes")
        if self.end <= self.start:
            raise ValueError("SearchWindow end must be after start")
