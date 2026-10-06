"""
actions/date_resolver.py
=====================================================================
Turns relative day phrases ("next Sunday", "tomorrow", "this weekend")
and a range of absolute date formats ("2026/12/16", "16th December")
into real Gregorian dates. Three places need this:
  - Trip planning's departure/return dates (Q3/Q4) — also used to
    calculate the actual number of nights for a hotel stay, instead
    of a fixed guess.
  - Weather Checking's "which day?" question — resolved to a date
    before calling data_services' weather endpoint.

Relative phrases are hand-coded (no library knows what "this weekend"
means without being told); absolute dates are handed off to
python-dateutil, which is far more robust than a hand-rolled regex for
the range of formats users actually type ("16th December", "Dec 16",
"2026-12-16", "16/12/2026", ...).
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Optional

from dateutil import parser as dateutil_parser
from dateutil.relativedelta import FR, MO, SA, SU, TH, TU, WE, relativedelta

_WEEKDAYS = {
    "monday": MO, "tuesday": TU, "wednesday": WE, "thursday": TH,
    "friday": FR, "saturday": SA, "sunday": SU,
}


def resolve_date(text: str, reference: Optional[date] = None) -> Optional[date]:
    """Best-effort resolution of a free-text day/date phrase into a real
    date. Returns None if nothing recognisable was found — the caller
    should ask the user to rephrase rather than silently guess.
    """
    if reference is None:
        reference = date.today()
    t = text.strip().lower()
    if not t or t.startswith("/"):
        # A structured button payload leaking in here unparsed (e.g. a
        # slot mapping missing its from_entity half) should fail loudly
        # as "not a date", never be scanned for a stray weekday name
        # that happens to appear inside the raw payload text — that's
        # exactly the bug this project hit once already.
        return None

    # ---- relative phrases, checked first (no library knows these) --------
    # Word-boundary matches throughout (not bare `in` substring checks) —
    # deliberately, so a phrase embedded in a longer, unrelated string
    # can never accidentally match.
    #
    # BUGFIX (real user report): "what is Berlin's weather today?" /
    # "what is weather of Tehran at the weekend?" — a combined sentence
    # naming the city AND the day together — never resolved a date at
    # all for "today"/"tomorrow"/"day after tomorrow", because these
    # three used re.fullmatch (the ENTIRE string had to be exactly
    # "today" and nothing else) while every other relative phrase below
    # already used re.search with \b boundaries (findable ANYWHERE in a
    # longer sentence). Switched to the same re.search + \b style for
    # consistency — "day after tomorrow" is checked BEFORE the bare
    # "tomorrow" search (unlike fullmatch, a substring search for
    # "tomorrow" alone would otherwise also match inside "day after
    # tomorrow" and return the wrong (+1 day) answer).
    if re.search(r"\b(the\s+)?day after tomorrow\b", t):
        return reference + timedelta(days=2)
    if re.search(r"\btomorrow\b", t):
        return reference + timedelta(days=1)
    if re.search(r"\b(today|this day)\b", t):
        return reference
    if re.search(r"\bthis weekend\b", t):
        # the coming Saturday — if today already IS the weekend, that's "now"
        if reference.weekday() >= 5:  # 5=Sat, 6=Sun
            return reference
        return reference + timedelta(days=(5 - reference.weekday()))
    if re.search(r"\bnext weekend\b", t):
        # BUGFIX (real user report + real debug log): departure resolved
        # to a Saturday (e.g. "this weekend"), then the return date
        # "next weekend" — resolved relative to that Saturday reference —
        # came out 14 days later instead of 7, silently doubling the
        # hotel stay's night count. Root cause: the old formula
        # `(5 - reference.weekday()) % 7`, when reference.weekday() == 5
        # (Saturday) itself, evaluates to 0, and the `else 7` fallback
        # (meant for a Sunday reference, where the coming Saturday is
        # genuinely still 6 days off after wrapping) was also firing for
        # a Saturday reference — treating "this coming Saturday" as 7
        # days out instead of recognizing the reference IS that Saturday,
        # so the final "+7 for next" landed 14 days out, not 7.
        #   Mirrors "this weekend"/bare "weekend" above (which already
        # special-case reference.weekday() >= 5): anchor to the Saturday
        # of the reference's OWN weekend first (itself, if reference is
        # already Saturday; one day back, if Sunday), then add exactly
        # one week for "next". A weekday (Mon-Fri) reference is completely
        # unaffected — same "days until the coming Saturday" as before.
        if reference.weekday() >= 5:
            this_saturday = reference - timedelta(days=reference.weekday() - 5)
        else:
            this_saturday = reference + timedelta(days=(5 - reference.weekday()))
        return this_saturday + timedelta(days=7)
    if re.search(r"\bweekend\b", t):
        # BUGFIX (real user report): a bare "weekend" — or "weekend"
        # embedded in a short phrase like "to weekend" — was falling
        # through both the "this weekend" and "next weekend" checks
        # above (neither matches without that exact qualifier word) and
        # then failing dateutil's parse too, leaving the user stuck in a
        # loop with no way to just say "weekend" and mean the nearest
        # one. Treated the same as "this weekend" — the nearest
        # upcoming Saturday — mirroring how a bare weekday name (e.g.
        # "Monday" with no "next") is already resolved to the closest
        # upcoming occurrence below.
        if reference.weekday() >= 5:
            return reference
        return reference + timedelta(days=(5 - reference.weekday()))
    if re.search(r"\bnext week\b", t):
        # "next week", or "next week this day" — same weekday, 7 days out
        return reference + timedelta(days=7)
    if re.search(r"\b(in )?two weeks\b", t):
        return reference + timedelta(days=14)

    for wd_name, wd_const in _WEEKDAYS.items():
        if re.search(rf"\b{wd_name}\b", t):
            # Both "next Monday" and a bare "Monday" resolve to the
            # closest UPCOMING occurrence (tomorrow..+7 days, never
            # today) — the simplest, least ambiguous reading of either.
            return reference + relativedelta(weekday=wd_const(+1))

    # ---- absolute dates: hand off to dateutil -----------------------------
    try:
        parsed = dateutil_parser.parse(text, fuzzy=True, dayfirst=False)
        return parsed.date()
    except (ValueError, OverflowError, TypeError):
        return None


def nights_between(start: date, end: date) -> Optional[int]:
    """Number of nights for a hotel stay between two dates. None (not 0
    or a negative number) if `end` isn't actually after `start` — the
    caller should treat that as an invalid answer and ask again, not
    silently book a zero/negative-night stay."""
    delta = (end - start).days
    return delta if delta > 0 else None


def format_date(d: date) -> str:
    """Consistent display format used everywhere a resolved date is
    shown back to the user."""
    return d.strftime("%Y-%m-%d")