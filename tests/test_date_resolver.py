"""
tests/test_date_resolver.py — relative/absolute date phrase resolution
=====================================================================
Run with:  pytest tests/ -v
"""

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from actions.date_resolver import format_date, nights_between, resolve_date

# Fixed reference date for reproducible tests: Tuesday, 2026-09-22
REF = date(2026, 9, 22)


class TestRelativePhrases:
    def test_today(self):
        assert resolve_date("today", reference=REF) == REF

    def test_tomorrow(self):
        assert resolve_date("tomorrow", reference=REF) == date(2026, 9, 23)

    def test_this_weekend_from_a_weekday(self):
        assert resolve_date("this weekend", reference=REF) == date(2026, 9, 26)  # Saturday

    def test_next_weekend(self):
        assert resolve_date("next weekend", reference=REF) == date(2026, 10, 3)

    def test_next_weekend_from_a_saturday_reference(self):
        # BUGFIX (real user report + real debug log): a departure date
        # that itself resolved to a Saturday (e.g. "this weekend"), used
        # as the reference for a return date of "next weekend", used to
        # land 14 days out instead of 7 — silently doubling a hotel
        # stay's night count (7 nights reported as 14). "This weekend"
        # from REF (Tue 2026-09-22) is Saturday 2026-09-26; "next
        # weekend" measured from THAT Saturday must be exactly 7 days
        # later, the very next Saturday — not 14.
        this_weekend = resolve_date("this weekend", reference=REF)
        assert this_weekend == date(2026, 9, 26)
        assert resolve_date("next weekend", reference=this_weekend) == date(2026, 10, 3)

    def test_next_weekend_from_a_sunday_reference(self):
        # Same bug, the other weekend day: a Sunday reference must anchor
        # to ITS OWN Saturday (the day before) before adding a week, not
        # wrap an extra 7 days further out.
        sunday_ref = date(2026, 9, 27)  # the Sunday of REF's "this weekend"
        assert resolve_date("next weekend", reference=sunday_ref) == date(2026, 10, 3)

    def test_next_week(self):
        assert resolve_date("next week", reference=REF) == date(2026, 9, 29)

    def test_bare_weekday_name(self):
        assert resolve_date("Friday", reference=REF) == date(2026, 9, 25)

    def test_next_weekday_name(self):
        assert resolve_date("next Sunday", reference=REF) == date(2026, 9, 27)


class TestAbsoluteDates:
    def test_iso_format(self):
        assert resolve_date("2026-12-16", reference=REF) == date(2026, 12, 16)

    def test_slash_format(self):
        assert resolve_date("2026/12/16", reference=REF) == date(2026, 12, 16)

    def test_day_month_name(self):
        assert resolve_date("16th December", reference=REF) == date(2026, 12, 16)

    def test_month_day_name(self):
        assert resolve_date("December 16 2026", reference=REF) == date(2026, 12, 16)

    def test_unparseable_text_returns_none(self):
        assert resolve_date("not a date at all", reference=REF) is None

    def test_empty_string_returns_none(self):
        assert resolve_date("", reference=REF) is None


class TestNightsBetween:
    def test_normal_range(self):
        assert nights_between(date(2026, 12, 16), date(2026, 12, 23)) == 7

    def test_same_day_is_invalid(self):
        assert nights_between(date(2026, 12, 16), date(2026, 12, 16)) is None

    def test_reversed_dates_is_invalid(self):
        assert nights_between(date(2026, 12, 20), date(2026, 12, 16)) is None


class TestFormatDate:
    def test_format(self):
        assert format_date(date(2026, 12, 16)) == "2026-12-16"


class TestEmbeddedRelativePhrases:
    """Regression tests for a real user report: "what is Berlin's weather
    today?" / "what is weather of Tehran at the weekend?" — a combined
    sentence naming the city AND the day together — never resolved a
    date at all for "today"/"tomorrow"/"day after tomorrow", because
    those three used re.fullmatch (the ENTIRE string had to be exactly
    that phrase) while every other relative phrase already used
    re.search with \\b boundaries (findable anywhere in a longer
    sentence)."""

    def test_today_embedded_in_a_question(self):
        assert resolve_date("what is Berlin's weather today?", reference=REF) == REF

    def test_tomorrow_embedded_in_a_question(self):
        assert resolve_date("what is the weather tomorrow in Rome?", reference=REF) == date(2026, 9, 23)

    def test_day_after_tomorrow_embedded_and_not_shadowed_by_bare_tomorrow(self):
        # "tomorrow" is a SUBSTRING of "day after tomorrow" — order
        # matters, or a naive substring search for "tomorrow" alone
        # would match first and return the wrong (+1 day) answer.
        assert resolve_date("weather the day after tomorrow please", reference=REF) == date(2026, 9, 24)

    def test_weekend_embedded_when_today_already_is_the_weekend(self):
        sunday_ref = date(2026, 9, 27)  # a Sunday
        assert resolve_date("what is weather of Tehran at the weekend?", reference=sunday_ref) == sunday_ref


class TestInTwoWeeksPhrase:
    """Regression test for a real, reported crash: the date-option button
    list had "next week" listed TWICE as a value (a copy-paste mistake —
    the "In two weeks" button's payload was accidentally "next week"),
    which crashed the Streamlit app with a DuplicateWidgetID error since
    two buttons in the same row ended up with identical keys."""

    def test_in_two_weeks_resolves_to_14_days_out(self):
        from datetime import date
        from actions.date_resolver import resolve_date
        ref = date(2026, 9, 26)
        assert resolve_date("in two weeks", reference=ref) == date(2026, 10, 10)
        assert resolve_date("two weeks", reference=ref) == date(2026, 10, 10)

    def test_date_option_values_have_no_duplicates(self):
        from actions.actions import _DATE_OPTION_LABELS, _DATE_OPTION_VALUES
        assert len(_DATE_OPTION_VALUES) == len(set(_DATE_OPTION_VALUES))
        assert len(_DATE_OPTION_LABELS) == len(_DATE_OPTION_VALUES)