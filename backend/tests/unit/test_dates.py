"""Date and time expression resolution.

These run against a fixed reference date so weekday arithmetic, month rollover
and "next week" semantics are pinned rather than dependent on when the suite
happens to run.
"""

from __future__ import annotations

from datetime import date, time

import pytest

from app.conversation.nlu.dates import (
    coming_weekday,
    parse_clock_time,
    resolve_date,
    resolve_time,
    weekday_next_week,
)
from app.core.timeutils import DayPeriod

# Wednesday 12 August 2026.
TODAY = date(2026, 8, 12)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("today", date(2026, 8, 12)),
        ("tomorrow", date(2026, 8, 13)),
        ("day after tomorrow", date(2026, 8, 14)),
        # Bare weekday: the coming one.
        ("friday", date(2026, 8, 14)),
        ("can I come in on monday", date(2026, 8, 17)),
        # "next Friday" said on a Wednesday means the Friday of next week.
        ("next friday", date(2026, 8, 21)),
        ("friday next week", date(2026, 8, 21)),
        ("this friday", date(2026, 8, 14)),
        ("the 21st", date(2026, 8, 21)),
        # An ordinal already past rolls into next month.
        ("the 3rd", date(2026, 9, 3)),
        ("the twenty first", date(2026, 8, 21)),
        ("21 august", date(2026, 8, 21)),
        ("august 21", date(2026, 8, 21)),
        ("2026-08-21", date(2026, 8, 21)),
        # Day-first, Australian convention.
        ("21/08", date(2026, 8, 21)),
        ("in 3 days", date(2026, 8, 15)),
        ("in two weeks", date(2026, 8, 26)),
    ],
)
def test_resolves_specific_days(text: str, expected: date) -> None:
    assert resolve_date(text, TODAY).day == expected


def test_a_date_already_passed_rolls_to_next_year() -> None:
    # 1 March is behind us in August, so it means next March.
    assert resolve_date("1 march", TODAY).day == date(2027, 3, 1)


def test_impossible_dates_are_rejected() -> None:
    assert resolve_date("31 february", TODAY).day is None


@pytest.mark.parametrize(
    ("text", "start", "end"),
    [
        ("next week", date(2026, 8, 17), date(2026, 8, 24)),
        ("this week", date(2026, 8, 12), date(2026, 8, 17)),
    ],
)
def test_resolves_ranges(text: str, start: date, end: date) -> None:
    resolution = resolve_date(text, TODAY)
    assert resolution.day is None
    assert (resolution.day_from, resolution.day_to) == (start, end)


def test_asap_produces_a_forward_window() -> None:
    resolution = resolve_date("as soon as possible", TODAY)
    assert resolution.day_from == TODAY
    assert resolution.day_to is not None and resolution.day_to > TODAY


def test_unrecognised_text_yields_nothing() -> None:
    assert resolve_date("i would like to see a doctor", TODAY).is_empty


@pytest.mark.parametrize(
    ("text", "period"),
    [
        ("morning", DayPeriod.MORNING),
        ("in the afternoon", DayPeriod.AFTERNOON),
        ("after lunch", DayPeriod.AFTERNOON),
        ("evening", DayPeriod.EVENING),
        ("tonight", DayPeriod.EVENING),
    ],
)
def test_resolves_periods(text: str, period: DayPeriod) -> None:
    assert resolve_time(text).period is period


def test_after_three_means_the_afternoon() -> None:
    """A bare hour in a clinic context is pm — nobody books 3am."""
    resolution = resolve_time("anything after 3")
    assert resolution.not_before == time(15, 0)
    assert resolution.period is DayPeriod.AFTERNOON


def test_after_an_explicit_morning_hour_stays_morning() -> None:
    resolution = resolve_time("after 10am")
    assert resolution.not_before == time(10, 0)
    assert resolution.period is DayPeriod.MORNING


def test_first_thing_is_early_morning() -> None:
    resolution = resolve_time("first thing please")
    assert resolution.period is DayPeriod.MORNING
    assert resolution.not_before == time(7, 0)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("10:30", time(10, 30)),
        ("10.30am", time(10, 30)),
        ("2 pm", time(14, 0)),
        ("the 4:15 one", time(16, 15)),
    ],
)
def test_parses_clock_times(text: str, expected: time) -> None:
    assert parse_clock_time(text) == expected


def test_weekday_helpers() -> None:
    # Wednesday -> the coming Wednesday is today itself.
    assert coming_weekday(TODAY, 2) == TODAY
    assert coming_weekday(TODAY, 2, include_today=False) == date(2026, 8, 19)
    assert weekday_next_week(TODAY, 0) == date(2026, 8, 17)


def test_ordinal_selection_is_not_read_as_a_date() -> None:
    """ "the first one" chooses an option; it is not the 1st of the month."""
    assert resolve_date("the first one", TODAY).day is None
    assert resolve_date("the second option", TODAY).day is None
    # But a plain ordinal still resolves as a date.
    assert resolve_date("the first", TODAY).day == date(2026, 9, 1)
