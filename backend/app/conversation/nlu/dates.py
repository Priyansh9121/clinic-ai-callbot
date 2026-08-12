"""Deterministic resolution of spoken date and time expressions.

This module is the *only* place a date expression becomes a concrete calendar
date. A language model may later be used to spot the expression in a sentence,
but the resolution and validation happen here, in code, against the clinic
timezone and a known reference instant. An LLM-produced date is never trusted.

Everything is pure and takes an explicit ``today``, so behaviour around
weekends, month ends and daylight saving is directly testable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, time, timedelta

from app.core.timeutils import DayPeriod

WEEKDAYS: dict[str, int] = {
    "monday": 0,
    "mon": 0,
    "tuesday": 1,
    "tue": 1,
    "tues": 1,
    "wednesday": 2,
    "wed": 2,
    "weds": 2,
    "thursday": 3,
    "thu": 3,
    "thur": 3,
    "thurs": 3,
    "friday": 4,
    "fri": 4,
    "saturday": 5,
    "sat": 5,
    "sunday": 6,
    "sun": 6,
}

MONTHS: dict[str, int] = {
    "january": 1,
    "jan": 1,
    "february": 2,
    "feb": 2,
    "march": 3,
    "mar": 3,
    "april": 4,
    "apr": 4,
    "may": 5,
    "june": 6,
    "jun": 6,
    "july": 7,
    "jul": 7,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sep": 9,
    "sept": 9,
    "october": 10,
    "oct": 10,
    "november": 11,
    "nov": 11,
    "december": 12,
    "dec": 12,
}

_NUMBER_WORDS: dict[str, int] = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
}

ORDINAL_WORDS: dict[str, int] = {
    "first": 1,
    "second": 2,
    "third": 3,
    "fourth": 4,
    "fifth": 5,
    "sixth": 6,
    "seventh": 7,
    "eighth": 8,
    "ninth": 9,
    "tenth": 10,
    "eleventh": 11,
    "twelfth": 12,
    "thirteenth": 13,
    "fourteenth": 14,
    "fifteenth": 15,
    "sixteenth": 16,
    "seventeenth": 17,
    "eighteenth": 18,
    "nineteenth": 19,
    "twentieth": 20,
    "twenty first": 21,
    "twenty second": 22,
    "twenty third": 23,
    "twenty fourth": 24,
    "twenty fifth": 25,
    "twenty sixth": 26,
    "twenty seventh": 27,
    "twenty eighth": 28,
    "twenty ninth": 29,
    "thirtieth": 30,
    "thirty first": 31,
}

_WEEKDAY_ALTERNATION = "|".join(sorted(WEEKDAYS, key=len, reverse=True))
_MONTH_ALTERNATION = "|".join(sorted(MONTHS, key=len, reverse=True))


@dataclass(frozen=True, slots=True)
class DateResolution:
    """A resolved date preference.

    ``day`` is None for a range expression such as "next week", where the
    caller has not named a specific day; the search then spans ``[day_from,
    day_to)``.
    """

    day: date | None = None
    day_from: date | None = None
    day_to: date | None = None
    #: The literal words that produced this, for logging and confirmation.
    expression: str | None = None
    #: True when the date came from an ordinal phrase ("the 21st", "the
    #: first"). Those are ambiguous with choosing from a list — "the first
    #: one" means option one, not the 1st of the month — so the analyser
    #: discards them when the caller is picking between offered slots.
    from_ordinal: bool = False

    @property
    def is_empty(self) -> bool:
        return self.day is None and self.day_from is None


@dataclass(frozen=True, slots=True)
class TimeResolution:
    period: DayPeriod = DayPeriod.ANY
    not_before: time | None = None
    expression: str | None = None

    @property
    def is_empty(self) -> bool:
        return self.period is DayPeriod.ANY and self.not_before is None


# --- Weekday arithmetic -----------------------------------------------------
def coming_weekday(today: date, weekday: int, *, include_today: bool = True) -> date:
    """The next occurrence of ``weekday``, today included by default."""
    delta = (weekday - today.weekday()) % 7
    if delta == 0 and not include_today:
        delta = 7
    return today + timedelta(days=delta)


def weekday_next_week(today: date, weekday: int) -> date:
    """The occurrence in the *following* calendar week (weeks start Monday).

    Australian English usage: "next Friday" said on a Tuesday means the Friday
    of next week, not this coming Friday. "Friday next week" is the same thing.
    """
    next_monday = today + timedelta(days=7 - today.weekday())
    return next_monday + timedelta(days=weekday)


# --- Date parsing -----------------------------------------------------------
def resolve_date(text: str, today: date) -> DateResolution:
    """Resolve the first date expression found in ``text``.

    Order matters: more specific patterns are tried first so that "Friday next
    week" is not consumed by the bare-weekday rule.
    """
    t = text.lower().strip()

    # --- explicit calendar dates ---
    iso = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", t)
    if iso:
        parsed = _safe_date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
        if parsed:
            return DateResolution(day=parsed, expression=iso.group(0))

    # Day/month, e.g. "21/8" or "21-08". Day-first: Australian convention.
    dmy = re.search(r"\b(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?\b", t)
    if dmy:
        day_n, month_n = int(dmy.group(1)), int(dmy.group(2))
        year_n = _expand_year(dmy.group(3), today)
        parsed = _safe_date(year_n, month_n, day_n)
        if parsed:
            return DateResolution(
                day=_roll_forward(parsed, today, dmy.group(3)), expression=dmy.group(0)
            )

    # "21 August" / "August 21st"
    dm = re.search(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({_MONTH_ALTERNATION})\b", t)
    if dm:
        parsed = _safe_date(today.year, MONTHS[dm.group(2)], int(dm.group(1)))
        if parsed:
            return DateResolution(day=_roll_forward(parsed, today, None), expression=dm.group(0))
    md = re.search(rf"\b({_MONTH_ALTERNATION})\s+(\d{{1,2}})(?:st|nd|rd|th)?\b", t)
    if md:
        parsed = _safe_date(today.year, MONTHS[md.group(1)], int(md.group(2)))
        if parsed:
            return DateResolution(day=_roll_forward(parsed, today, None), expression=md.group(0))

    # --- relative days ---
    if re.search(r"\b(today|this morning|this afternoon|this evening|tonight)\b", t):
        return DateResolution(day=today, expression="today")
    # Checked before the bare "tomorrow" rule, which would otherwise consume it.
    if re.search(r"\bday after tomorrow\b", t):
        return DateResolution(day=today + timedelta(days=2), expression="day after tomorrow")
    if re.search(r"\b(tomorrow|tomorow|tmrw)\b", t):
        return DateResolution(day=today + timedelta(days=1), expression="tomorrow")

    in_days = re.search(
        r"\bin (\d+|a|an|" + "|".join(_NUMBER_WORDS) + r") (day|days|week|weeks)\b", t
    )
    if in_days:
        count = _word_to_int(in_days.group(1))
        if count is not None:
            days = count * (7 if in_days.group(2).startswith("week") else 1)
            return DateResolution(day=today + timedelta(days=days), expression=in_days.group(0))

    # --- weekday expressions (most specific first) ---
    m = re.search(rf"\b({_WEEKDAY_ALTERNATION})\s+(?:of\s+)?next week\b", t)
    if m:
        return DateResolution(
            day=weekday_next_week(today, WEEKDAYS[m.group(1)]), expression=m.group(0)
        )
    m = re.search(rf"\bnext\s+({_WEEKDAY_ALTERNATION})\b", t)
    if m:
        return DateResolution(
            day=weekday_next_week(today, WEEKDAYS[m.group(1)]), expression=m.group(0)
        )
    m = re.search(rf"\b(?:this|coming)\s+({_WEEKDAY_ALTERNATION})\b", t)
    if m:
        return DateResolution(
            day=coming_weekday(today, WEEKDAYS[m.group(1)]), expression=m.group(0)
        )
    m = re.search(rf"\b({_WEEKDAY_ALTERNATION})\b", t)
    if m:
        return DateResolution(
            day=coming_weekday(today, WEEKDAYS[m.group(1)]), expression=m.group(0)
        )

    # --- "the 21st" / "the twenty first" ---
    # A trailing "one"/"option"/"slot" makes it a choice from a list, not a
    # date: "the first one", "the 2nd option".
    _not_a_choice = r"(?!\s+(one|option|slot|time|appointment)\b)"
    m = re.search(rf"\b(?:on\s+)?the\s+(\d{{1,2}})(?:st|nd|rd|th)\b{_not_a_choice}", t)
    if m:
        resolved = _next_month_day(int(m.group(1)), today)
        if resolved:
            return DateResolution(day=resolved, expression=m.group(0), from_ordinal=True)
    ordinal_alt = "|".join(sorted(ORDINAL_WORDS, key=len, reverse=True))
    m = re.search(rf"\bthe\s+({ordinal_alt})\b{_not_a_choice}", t)
    if m:
        resolved = _next_month_day(ORDINAL_WORDS[m.group(1)], today)
        if resolved:
            return DateResolution(day=resolved, expression=m.group(0), from_ordinal=True)

    # --- ranges ---
    if re.search(r"\bnext week\b", t):
        start = today + timedelta(days=7 - today.weekday())
        return DateResolution(
            day_from=start, day_to=start + timedelta(days=7), expression="next week"
        )
    if re.search(r"\bthis week\b", t):
        return DateResolution(
            day_from=today,
            day_to=today + timedelta(days=7 - today.weekday()),
            expression="this week",
        )
    if re.search(r"\b(next month|in a month)\b", t):
        start = today + timedelta(days=30)
        return DateResolution(
            day_from=start, day_to=start + timedelta(days=14), expression="next month"
        )
    if re.search(
        r"\b(as soon as possible|asap|earliest|anytime|any time|whenever|"
        r"first available|soonest)\b",
        t,
    ):
        return DateResolution(
            day_from=today, day_to=today + timedelta(days=14), expression="as soon as possible"
        )

    return DateResolution()


# --- Time-of-day parsing ----------------------------------------------------
def resolve_time(text: str, *, base_period: DayPeriod = DayPeriod.ANY) -> TimeResolution:
    """Resolve a period and/or an earliest acceptable time."""
    t = text.lower().strip()
    period = base_period
    not_before: time | None = None
    expression: str | None = None

    if re.search(r"\b(morning|before lunch|before noon|am)\b", t):
        period, expression = DayPeriod.MORNING, "morning"
    if re.search(r"\b(afternoon|after lunch|arvo|pm)\b", t):
        period, expression = DayPeriod.AFTERNOON, "afternoon"
    if re.search(r"\b(evening|tonight|after work|late in the day)\b", t):
        period, expression = DayPeriod.EVENING, "evening"

    if re.search(r"\b(first thing|early|as early as possible|start of the day)\b", t):
        period = DayPeriod.MORNING if period is DayPeriod.ANY else period
        not_before, expression = time(7, 0), "first thing"

    # "after 3", "after 3pm", "after half past two", "anything after 10:30"
    m = re.search(r"\bafter\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", t)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2) or 0)
        meridiem = m.group(3)
        hour = _to_24h(hour, meridiem, assume_pm_below=8)
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            not_before = time(hour, minute)
            expression = m.group(0)
            if period is DayPeriod.ANY:
                period = (
                    DayPeriod.MORNING
                    if hour < 12
                    else (DayPeriod.AFTERNOON if hour < 17 else DayPeriod.EVENING)
                )

    return TimeResolution(period=period, not_before=not_before, expression=expression)


def parse_clock_time(text: str) -> time | None:
    """Parse a bare clock time such as "10:30", "10.30am" or "2 pm".

    Used to let a caller pick an offered slot by saying its time rather than
    "the first one".
    """
    t = text.lower()
    m = re.search(r"\b(\d{1,2})[:.](\d{2})\s*(am|pm)?\b", t)
    if m:
        hour = _to_24h(int(m.group(1)), m.group(3), assume_pm_below=8)
        minute = int(m.group(2))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return time(hour, minute)
    m = re.search(r"\b(\d{1,2})\s*(am|pm)\b", t)
    if m:
        hour = _to_24h(int(m.group(1)), m.group(2), assume_pm_below=8)
        if 0 <= hour <= 23:
            return time(hour, 0)
    return None


# --- Internals --------------------------------------------------------------
def _to_24h(hour: int, meridiem: str | None, *, assume_pm_below: int) -> int:
    """Convert to 24-hour. Without a meridiem, a small hour is read as pm.

    "after 3" in a clinic context means 3 in the afternoon; a caller wanting
    3 am would not be booking a GP appointment.
    """
    if meridiem == "pm":
        return hour if hour == 12 else hour + 12
    if meridiem == "am":
        return 0 if hour == 12 else hour
    return hour + 12 if hour < assume_pm_below else hour


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None  # e.g. 31 February


def _expand_year(raw: str | None, today: date) -> int:
    if not raw:
        return today.year
    value = int(raw)
    if value < 100:
        return 2000 + value
    return value


def _roll_forward(parsed: date, today: date, explicit_year: str | None) -> date:
    """A date without a year that has already passed means next year."""
    if explicit_year or parsed >= today:
        return parsed
    try:
        return parsed.replace(year=parsed.year + 1)
    except ValueError:  # 29 February
        return parsed + timedelta(days=365)


def _next_month_day(day_number: int, today: date) -> date | None:
    """ "the 21st" — this month if still ahead, otherwise next month."""
    if not 1 <= day_number <= 31:
        return None
    candidate = _safe_date(today.year, today.month, day_number)
    if candidate and candidate >= today:
        return candidate
    year, month = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
    return _safe_date(year, month, day_number)


def _word_to_int(word: str) -> int | None:
    if word.isdigit():
        return int(word)
    if word in ("a", "an"):
        return 1
    return _NUMBER_WORDS.get(word)
