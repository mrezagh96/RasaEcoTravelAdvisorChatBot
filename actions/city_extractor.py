"""
actions/city_extractor.py
=====================================================================
Pulls a city-name CANDIDATE out of raw free text using regex patterns
around common trigger words ("to", "to go to", "from"), rather than
relying solely on DIET's trained entity extraction — this is what
lets the bot handle a city it's never seen a training example for
(the same problem "Tabriz" and word-order variants exposed earlier).

The candidate this module returns is NOT validated — it's just "the
most likely span of text that names a place". Fuzzy-matching that
candidate against the curated city list (and deciding whether it
needs a "Did you mean?" confirmation) is data_services/geocode.py's
job; actions.py wires the two together.

Design notes on the regex itself:
  * Multi-word cities ("New York", "Los Angeles") are handled by
    capturing a RUN of Title-Case words after the trigger, stopping at
    the first lowercase word/punctuation — not just one word.
  * Patterns are checked most-specific-first ("to go to X" before
    "to X") so a longer phrase doesn't get cut short.
  * Two flavours are exposed: a STRICT trigger-only extractor (safe to
    run unconditionally on any message, since it can never misfire on
    unrelated text) and a lenient one that additionally falls back to
    "the whole message is the city name" — that lenient fallback is
    only safe to use when the bot is DIRECTLY asking for this exact
    slot right now (see actions.py's extract_origin_city /
    extract_destination_city for that guard).

  * A THIRD flavour — extract_destination_trigger_lenient /
    extract_origin_trigger_lenient / extract_city_trigger_lenient
    (added per a real user report: casual, all-lowercase phrasing like
    "i wanna go to dubai" wasn't recognised at all, since the strict
    patterns above require the city to start with a Capital Letter, the
    one signal they use to know where the city name ends). These match
    the SAME trigger words case-insensitively and accept a lowercase
    city span too, capped at 2 words (covers the vast majority of real
    city names — "New York", "Sao Paulo" — without also swallowing
    trailing filler like "please"/"thanks"/"today", which is stripped
    off the end of whatever was captured). Because dropping the
    Capital-Letter requirement removes the one thing that kept the
    strict extractor "safe to run on any message" (a stray "to work" or
    "from home" would otherwise get treated as a city candidate), these
    lenient variants are ONLY ever called from actions.py when the form
    is DIRECTLY asking for that exact slot right now — same rule as the
    existing whole-message fallback below, never run unconditionally on
    every turn the way the strict extractors are.
"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

from . import date_resolver

_CITY_WORD = r"[A-Z][A-Za-z'’\-]*"
_CITY_PHRASE = rf"({_CITY_WORD}(?:\s+{_CITY_WORD})*)"

# Ordered most-specific first — "to go to X" must be tried before the
# bare "to X" pattern, or the shorter pattern would (harmlessly, but
# uselessly) match first and potentially grab a less complete phrase.
_DESTINATION_PATTERNS = [
    rf"(?i:to\s+go\s+to)\s+{_CITY_PHRASE}",
    rf"(?i:to\s+go)\s+{_CITY_PHRASE}",
    rf"(?i:travel(?:ling)?\s+to)\s+{_CITY_PHRASE}",
    rf"(?i:to)\s+{_CITY_PHRASE}",
]

_ORIGIN_PATTERNS = [
    rf"(?i:leaving\s+from)\s+{_CITY_PHRASE}",
    rf"(?i:from)\s+{_CITY_PHRASE}",
]


def _first_match(text: str, patterns: list) -> Optional[str]:
    for pattern in patterns:
        m = re.search(pattern, text)
        if m:
            return m.group(1).strip()
    return None


def _is_date_phrase(phrase: str) -> bool:
    """True if `phrase` (already trimmed to a candidate "city" span) is
    itself something date_resolver can resolve to a real date/day —
    "the weekend", "next Monday", "today", "tomorrow", ... None of
    these are ever city names, so any trigger-word match that
    captured one of these is a false positive, not a real candidate.
    """
    return date_resolver.resolve_date(phrase) is not None


def _is_bare_trigger_word(phrase: str) -> bool:
    """True if the candidate, taken as a WHOLE (not just a trailing
    word already handled by _strip_trailing_filler), is itself one of
    the trigger/filler words in _LENIENT_STOP_WORDS.

    BUGFIX (regression caught by tests/test_form_bugs.py after this
    round's other fixes): "I want to go to travel" — an off-topic
    sentence with no destination in it at all — matches the "to go
    to X" pattern's OWN trigger phrase so completely that the single
    word left over, "travel", looks like a valid one-word candidate.
    No real city is ever literally the word "travel", "to", "from",
    "going", etc., so a candidate that reduces to exactly one of
    these (case-insensitively) is rejected the same way a date phrase
    is — not returned, and the caller moves on to the next
    match/pattern instead."""
    return phrase.strip().lower() in _LENIENT_STOP_WORDS


# BUGFIX (real user report + real debug log, confirmed via real
# execution): "I want to go to a travel from tomorrow to next week
# from berlin to lisbon" resolved destination_city to an unrelated
# real place ("Salta") instead of "Lisbon". Root cause, traced with
# city_extractor.extract_destination_trigger_lenient() called directly
# against this exact sentence: the "to go to X" pattern's own trigger
# phrase ends at "...go to ", leaving "a travel" as the remainder: "a"
# is captured as the phrase's first word (it was NOT in
# _LENIENT_STOP_WORDS, unlike "go"/"travel", which the ORIGINAL fix
# above already blocks as the second word), so the one-word candidate
# "a" is returned. That candidate then reaches
# actions.py's _match_city_or_ask -> api_clients.match_city("a") ->
# data_services' /geocode/suggest, which — once curated-list fuzzy
# matching (difflib, correctly) finds nothing for a single letter —
# falls through to a LIVE Nominatim lookup for the literal query "a".
# Nominatim's free-text search has no minimum-length floor either, so
# an under-constrained one-letter query can non-deterministically
# resolve to whatever place its own ranking picks that day (confirmed
# via a real, live call: it returned "not found" in one run and could
# just as easily return a real but totally unrelated settlement like
# "Salta" in another — this is exactly the failure mode reported).
#
# No real city name is ever a bare English article, and no real city
# name is ever a single character, so both are rejected here — the
# same "reduces to a known non-city word" principle as
# _is_bare_trigger_word above, just widened to catch this specific
# miss and to close off the general case (any other stray 1-character
# candidate) at the same time, rather than only patching "a" by name.
_LENIENT_ARTICLE_WORDS = {"a", "an", "the"}


def _is_implausible_city_length(phrase: str) -> bool:
    """True for a candidate too short to plausibly be a real city name
    (a bare English article, or anything reduced to a single
    character) — see the BUGFIX note above _LENIENT_ARTICLE_WORDS."""
    normalised = phrase.strip().lower()
    if normalised in _LENIENT_ARTICLE_WORDS:
        return True
    return len(normalised.replace(" ", "")) < 2


def _is_bad_candidate(phrase: str) -> bool:
    return (
        _is_date_phrase(phrase)
        or _is_bare_trigger_word(phrase)
        or _is_implausible_city_length(phrase)
    )


def _first_non_date_match(text: str, patterns: list) -> Optional[str]:
    """Like _first_match, but a candidate that resolves as a date/day
    phrase, or is itself just a bare trigger/filler word (see
    _is_bad_candidate), is skipped (trying the NEXT pattern in
    priority order) rather than returned. Guards the same bug class
    as _leftmost_non_date_match below for the destination/origin
    patterns, where a date phrase appearing right after "to"/"from"
    could otherwise be mistaken for a city (e.g. a stray "to
    tomorrow" fragment)."""
    for pattern in patterns:
        for m in re.finditer(pattern, text):
            candidate = m.group(1).strip()
            cleaned = _strip_trailing_filler(candidate)
            if cleaned and not _is_bad_candidate(cleaned):
                return cleaned
    return None


def _leftmost_non_date_match(text: str, pattern_groups: List[list]) -> Optional[str]:
    """Collects every match across ALL the given pattern lists (e.g.
    the "in X" / "for X" / "at X" / "of X" generic patterns, plus the
    possessive "X's weather" pattern), together with WHERE each match
    starts in the original text, and returns the CLEANED text of
    whichever one starts EARLIEST — not whichever pattern happens to
    be first in the list.

    BUGFIX (real user report): "what is weather of Tehran at the
    weekend?" — the fixed pattern-priority order (in, for, at, of)
    made the "at X" pattern match "at the weekend" (matching the
    trigger word "at" that happens to precede an unrelated date
    phrase later in the sentence) BEFORE the "of X" pattern ever got
    a chance to match "of Tehran" — even though "of Tehran" starts
    earlier in the actual sentence. A fixed trigger-word priority
    list can't tell "the real city's trigger word" apart from "some
    other trigger word that happens to precede a date phrase";
    scanning left-to-right through the sentence and taking whichever
    real candidate appears first can. Candidates that resolve as a
    date/day phrase, or a bare trigger/filler word (see
    _is_bad_candidate), are skipped entirely (not just deprioritised)
    so "at the weekend" is never returned even if it were the only
    match in a message.
    """
    candidates: List[Tuple[int, str]] = []
    for patterns in pattern_groups:
        for pattern in patterns:
            for m in re.finditer(pattern, text):
                candidates.append((m.start(1), m.group(1).strip()))
    candidates.sort(key=lambda pair: pair[0])
    for _, candidate in candidates:
        cleaned = _strip_trailing_filler(candidate)
        if cleaned and not _is_bad_candidate(cleaned):
            return cleaned
    return None


def extract_destination_trigger(text: str) -> Optional[str]:
    """Destination city ONLY if an explicit trigger phrase ("to", "to go
    to", ...) matched — returns None otherwise, with NO whole-message
    fallback. Safe to run unconditionally on any message, since it can
    never misfire on unrelated text that happens not to contain a
    trigger phrase."""
    return _first_match(text, _DESTINATION_PATTERNS)


def extract_origin_trigger(text: str) -> Optional[str]:
    """Same idea for origin, keyed on "from" / "leaving from"."""
    return _first_match(text, _ORIGIN_PATTERNS)


# ---- lenient (any-case) trigger matching — see the module docstring's
# "THIRD flavour" note above for why this is a separate, more carefully
# gated set of helpers rather than a change to the strict ones above. ---

_LENIENT_CITY_WORD = r"[A-Za-z][A-Za-z'’\-]*"

# BUGFIX (real user report): "I want to go to frankfurt from berlin" —
# an all-lowercase sentence combining BOTH destination and origin in
# one go — was resolving to a destination candidate of "frankfurt from"
# instead of just "frankfurt". Root cause: the 2-word cap below used a
# bare `[A-Za-z]+` word class for the OPTIONAL second word, with no idea
# that "from" is itself a TRIGGER word starting the next clause, not
# part of the city name. This stop-word list is checked with a negative
# lookahead so the phrase's optional second word can never BE one of
# these — "frankfurt from" now only ever captures "frankfurt". Shared
# across destination/origin/generic patterns below since a trigger word
# for ANY of them should stop ALL of them from over-capturing.
#
# BUGFIX (real user report): "check weather in Rome next Sunday" was
# capturing "Rome next" as the city — "next" wasn't in this list, so
# nothing stopped it being swallowed as the phrase's optional 2nd
# word. Added the day/date-ish words below (weekday names, "next",
# "this", "last", "weekend(s)", "week(s)", "month(s)", "year(s)",
# "day(s)", the parts of a day) for the same reason: no real city
# name is ever "<City> Next" / "<City> Weekend" / "<City> Monday".
#
# BUGFIX (regression in tests/test_form_bugs.py once the trigger
# match below started running unconditionally): "I want to go to
# travel" — an off-topic sentence naming no city at all — matches the
# "to go to X" pattern so fully that the leftover word "travel" (or,
# once that's blocked, "go" from a shorter "to X" match against the
# sentence's OWN inner "to go") looked like a valid one-word city.
# Added "go" alongside the already-present "going"/"travel" so this
# bare verb can never be captured as a candidate either — see
# _LENIENT_CITY_PHRASE below, which now blocks stop words as the
# phrase's FIRST word too, not just its optional second one.
#
# BUGFIX (real user report + real debug log): "check Tehran wheather
# for today" — a bare "<City> weather" compound noun with NO trigger
# word (in/for/at/of/'s) anywhere near the city at all — failed to
# extract "Tehran" entirely (see _LENIENT_BARE_WEATHER_PATTERNS
# below, which needed this). "weather"/"forecast" (and the exact
# real typo reported, "wheather") are added here so the shared
# 2-word-city-phrase capture never swallows the weather word ITSELF
# as if it were part of a 2-word city name (e.g. "Tehran wheather"
# captured whole, instead of just "Tehran").
_LENIENT_STOP_WORDS = (
    "to", "from", "leaving", "coming", "starting", "going", "go",
    "travel", "travelling", "traveling", "heading", "flying",
    "in", "for", "at", "of", "on", "by",
    "please", "thanks", "thank", "you", "now", "today", "tomorrow",
    "and", "or", "but", "so", "ok", "okay", "yeah", "yes", "no",
    "next", "this", "last", "weekend", "weekends", "week", "weeks",
    "month", "months", "year", "years", "day", "days",
    "morning", "afternoon", "evening", "night", "nights", "tonight",
    "monday", "tuesday", "wednesday", "thursday", "friday",
    "saturday", "sunday",
    "weather", "wheather", "forecast",
    # Added for the destination-CHANGE trigger patterns below ("change
    # the destination to Rome instead", "actually, Vienna instead") —
    # no real city is ever literally "instead", and without this the
    # shared 2-word capture could swallow it as a phrase's 2nd word
    # ("Rome instead").
    "instead",
)
_LENIENT_STOP_WORDS_RE = "|".join(_LENIENT_STOP_WORDS)
# Capped at 2 words — covers "Dubai", "New York", "Sao Paulo", ... without
# also reaching for a 3rd, unrelated trailing word in a full sentence —
# and NEITHER word is ever one of the stop words above.
#
# The FIRST word's block (added alongside the second word's, which
# was already there) is what fixes "I want to go to travel" /
# "...to go" matching a stray "go" or "travel" as if it were the city
# right after the trigger: since these patterns run unconditionally
# now (no longer gated to "the bot is asking for this slot right
# now"), the assumption that "whatever comes right after a trigger
# word is safely the city" no longer holds when the SAME sentence
# contains more than one trigger-like word in a row (as in "to go
# to"). Blocking known non-city glue words from ever being the
# phrase's first word closes that gap without weakening the
# already-working 2-word city names ("New York", "Sao Paulo", ...).
_LENIENT_CITY_PHRASE = (
    rf"((?!(?:{_LENIENT_STOP_WORDS_RE})\b){_LENIENT_CITY_WORD}"
    rf"(?:\s+(?!(?:{_LENIENT_STOP_WORDS_RE})\b){_LENIENT_CITY_WORD}){{0,1}})"
)

_LENIENT_DESTINATION_PATTERNS = [
    rf"(?i:to\s+go\s+to)\s+{_LENIENT_CITY_PHRASE}",
    rf"(?i:to\s+go)\s+{_LENIENT_CITY_PHRASE}",
    rf"(?i:travel(?:ling)?\s+to)\s+{_LENIENT_CITY_PHRASE}",
    rf"(?i:heading\s+to)\s+{_LENIENT_CITY_PHRASE}",
    rf"(?i:flying\s+to)\s+{_LENIENT_CITY_PHRASE}",
    rf"(?i:going\s+to)\s+{_LENIENT_CITY_PHRASE}",
    rf"(?i:\bto\b)\s+{_LENIENT_CITY_PHRASE}",
]

_LENIENT_ORIGIN_PATTERNS = [
    rf"(?i:leaving\s+from)\s+{_LENIENT_CITY_PHRASE}",
    rf"(?i:coming\s+from)\s+{_LENIENT_CITY_PHRASE}",
    rf"(?i:starting\s+from)\s+{_LENIENT_CITY_PHRASE}",
    rf"(?i:\bfrom\b)\s+{_LENIENT_CITY_PHRASE}",
]

# A generic version for the single-city questions (weather, hotel list)
# that don't have an origin/destination distinction to make.
_LENIENT_GENERIC_PATTERNS = [
    rf"(?i:\bin\b)\s+{_LENIENT_CITY_PHRASE}",
    rf"(?i:\bfor\b)\s+{_LENIENT_CITY_PHRASE}",
    rf"(?i:\bat\b)\s+{_LENIENT_CITY_PHRASE}",
    # "of" (real user report: "check the weather OF Tehran", "weather
    # OF Tehran at the weekend") — the single most common way people
    # phrase a weather/hotel-list question around a city name.
    rf"(?i:\bof\b)\s+{_LENIENT_CITY_PHRASE}",
]

# Possessive phrasing ("Berlin's weather", "what's Tehran's forecast?")
# — the city comes BEFORE the trigger word here, the reverse of every
# pattern above, so it needs its own shape rather than fitting the
# "trigger then city" template.
#
# BUGFIX (real user report): "what is Berlin's weather today?" was
# capturing "is Berlin" as the city, not "Berlin". _LENIENT_CITY_PHRASE
# was designed for the "trigger word then city" patterns above, where
# the trigger word ("to"/"from"/"in"/...) is consumed OUTSIDE the
# capture group, so its mandatory first word is always safely the
# start of the actual city phrase. The possessive pattern has no such
# trigger word in front — the capture starts wherever the regex engine
# tries next — so its mandatory "first word" can end up being an
# ordinary sentence word ("is", "what", "the", ...) that merely
# happens to sit right before a 2-word span ending in "'s weather".
# This dedicated phrase adds a negative lookahead in front of the
# FIRST word too (a small dedicated list, since ordinary sentence
# glue words like "is"/"what"/"the" aren't day/date words and so
# aren't in _LENIENT_STOP_WORDS), so a match starting on one of those
# words is rejected outright and re.search backtracks to the next
# start position — which correctly lands on "Berlin".
#
# That alone isn't quite enough: re.search tries EVERY start
# position, including ones in the MIDDLE of a blocked word — e.g.
# "is" blocks a match starting at "i", but nothing stopped one
# starting one character later at the bare "s", which then greedily
# grabbed "s Berlin" instead. Two more guards close that off: a
# leading \b requires the match to start on an actual word boundary
# (blocks starting mid-word, like "s" inside "is"), and a negative
# lookbehind for a preceding apostrophe blocks starting right after a
# contraction's apostrophe (like the "s" in "what's" — which IS a
# word boundary, since ' is a non-word character, so \b alone
# wouldn't catch it).
_LENIENT_POSSESSIVE_LEAD_STOP_WORDS = (
    "is", "was", "are", "were", "what", "whats", "how", "hows",
    "does", "do", "did", "the", "a", "an", "my", "your", "his",
    "her", "its", "our", "their", "this", "that", "check", "tell",
    "know", "give", "whats",
    "isn't", "wasn't", "aren't", "weren't", "doesn't", "don't",
    "didn't", "wouldn't", "couldn't", "shouldn't", "hasn't",
    "haven't", "hadn't",
)
_LENIENT_POSSESSIVE_LEAD_STOP_WORDS_RE = "|".join(_LENIENT_POSSESSIVE_LEAD_STOP_WORDS)
_LENIENT_POSSESSIVE_CITY_PHRASE = (
    rf"(?<!['’])\b((?!(?:{_LENIENT_POSSESSIVE_LEAD_STOP_WORDS_RE})\b){_LENIENT_CITY_WORD}"
    rf"(?:\s+(?!(?:{_LENIENT_STOP_WORDS_RE})\b){_LENIENT_CITY_WORD}){{0,1}})"
)
_LENIENT_POSSESSIVE_PATTERNS = [
    rf"{_LENIENT_POSSESSIVE_CITY_PHRASE}'s\s+(?i:weather|forecast)",
]

# Bare "<City> weather" / "<City> forecast" compound noun — no trigger
# word AND no possessive "'s" either (the reverse of every pattern
# above: "check Tehran wheather for today", "Rome forecast please").
#
# BUGFIX (real user report + real debug log): this exact sentence
# failed to extract "Tehran" at all under every pattern that existed
# before this fix — there's no "in"/"for"/"at"/"of" anywhere near the
# city, and no possessive "'s" either, so neither
# _LENIENT_GENERIC_PATTERNS nor _LENIENT_POSSESSIVE_PATTERNS could
# ever match it. Reuses _LENIENT_POSSESSIVE_CITY_PHRASE (not
# _LENIENT_CITY_PHRASE) for the SAME reason the possessive pattern
# needed its own phrase shape: there's no trigger word consumed
# OUTSIDE the capture group to anchor where the city starts, so the
# same lead-word stoplist guard is needed here too (it already blocks
# "check", "the", "is", etc. — see that pattern's own long comment
# above for why). The literal "'s" from the possessive pattern is
# simply replaced with plain whitespace, and the reported real typo
# ("wheather") is accepted alongside the correct spelling — narrowly,
# only this one confirmed real typo, not a general fuzzy-spell-check,
# to avoid over-matching unrelated text.
_LENIENT_BARE_WEATHER_PATTERNS = [
    rf"{_LENIENT_POSSESSIVE_CITY_PHRASE}\s+(?i:weather|wheather|forecast)\b",
]

# Common words that can end up right after a trigger in a full sentence
# but are never part of a city name — stripped off the END of whatever
# the lenient patterns captured (never the start, so a real 2-word city
# name is never cut down to one word). Mostly superseded by the stop-word
# lookahead above (which stops these from being captured as the 2nd word
# in the first place), kept as a second line of defence for anything
# that lookahead doesn't cover.
_TRAILING_FILLER_WORDS = {
    "please", "thanks", "thank", "you", "now", "today", "tomorrow",
    "for", "me", "and", "or", "but", "so", "ok", "okay", "yeah", "yes",
}


def _strip_trailing_filler(phrase: str) -> str:
    words = phrase.split()
    while words and words[-1].lower().rstrip(",.!?;:") in _TRAILING_FILLER_WORDS:
        words.pop()
    return " ".join(words)


def extract_destination_trigger_lenient(text: str) -> Optional[str]:
    """Same as extract_destination_trigger, but case-insensitive on the
    city name too — catches casual, all-lowercase phrasing like "i wanna
    go to dubai" AND a combined sentence like "i want to go to frankfurt
    from berlin" (the stop-word lookahead in _LENIENT_CITY_PHRASE stops
    the capture at "from", so this no longer swallows into the next
    clause — a real, reproduced bug this fixes, not a hypothetical).

    Safe to call UNCONDITIONALLY on any message now, unlike the bare
    "assume the whole message is the city name" fallback a caller might
    use when this returns None — that fallback still needs the "only
    when this exact slot is being asked right now" guard, but matching
    an explicit TRIGGER WORD ("to X") here can't misfire on unrelated
    text the way a bare whole-message guess could.

    Also skips a candidate that resolves as a date/day phrase (see
    _is_date_phrase) — e.g. a stray "to tomorrow" fragment — trying
    the next pattern in priority order instead of returning it."""
    return _first_non_date_match(text, _LENIENT_DESTINATION_PATTERNS)


def extract_origin_trigger_lenient(text: str) -> Optional[str]:
    """Same idea for origin — also now safe to call unconditionally
    (see extract_destination_trigger_lenient's docstring)."""
    return _first_non_date_match(text, _LENIENT_ORIGIN_PATTERNS)


def extract_city_trigger_lenient(text: str) -> Optional[str]:
    """Generic "in X" / "for X" / "at X" / "of X" version for the
    single-city questions (weather checking, hotel list), plus the
    possessive "Berlin's weather" / "Tehran's forecast" phrasing, plus
    the bare "Tehran weather" / "Rome forecast" compound-noun phrasing
    (see _LENIENT_BARE_WEATHER_PATTERNS) — same case-insensitive,
    filler-stripped matching. Safe to call unconditionally (see
    extract_destination_trigger_lenient's docstring); only a bare
    whole-message fallback afterward needs the "only when this exact
    slot is being asked" guard.

    BUGFIX (real user report): "what is weather of Tehran at the
    weekend?" used to return "the weekend" as the "city" — the fixed
    trigger-word priority order (in, for, at, of) let "at the
    weekend" win over "of Tehran" even though "of Tehran" appears
    EARLIER in the sentence. Now scans left-to-right across ALL of
    these patterns together (see _leftmost_non_date_match) and skips
    any candidate that resolves as a date/day phrase, so "of Tehran"
    is correctly preferred and "the weekend" is never returned as a
    city.

    BUGFIX (real user report + real debug log): "check Tehran
    wheather for today" — no trigger word and no possessive "'s" at
    all — used to return nothing whatsoever. Now also tries the bare
    compound-noun pattern group."""
    return _leftmost_non_date_match(
        text,
        [_LENIENT_GENERIC_PATTERNS, _LENIENT_POSSESSIVE_PATTERNS, _LENIENT_BARE_WEATHER_PATTERNS],
    )


def extract_destination_candidate(text: str) -> str:
    """Best-guess destination city span from free text. Falls back to
    the whole trimmed message if no trigger phrase matches. Only call
    this when the bot is DIRECTLY asking for a destination right now
    (see ValidateTripPlanningForm.extract_destination_city) — the bare
    fallback isn't safe to apply to an arbitrary message that might be
    about something else entirely."""
    match = extract_destination_trigger(text)
    return match if match else text.strip()


def extract_origin_candidate(text: str) -> str:
    """Same idea for origin city, keyed on 'from' rather than 'to'."""
    match = extract_origin_trigger(text)
    return match if match else text.strip()


_LENIENT_DESTINATION_CHANGE_PATTERNS = [
    rf"(?i:change\s+(?:it|the\s+destination|my\s+destination)\s+to)\s+{_LENIENT_CITY_PHRASE}",
    rf"(?i:switch\s+(?:it|the\s+destination)\s+to)\s+{_LENIENT_CITY_PHRASE}",
    rf"(?i:update\s+the\s+destination(?:\s+to)?\s+to)\s+{_LENIENT_CITY_PHRASE}",
    rf"(?i:make\s+it)\s+{_LENIENT_CITY_PHRASE}",
    rf"(?i:take\s+me\s+to)\s+{_LENIENT_CITY_PHRASE}",
    rf"(?i:travel(?:ling)?\s+to)\s+{_LENIENT_CITY_PHRASE}",
    rf"(?i:go(?:ing)?\s+to)\s+{_LENIENT_CITY_PHRASE}",
    rf"(?i:visit)\s+{_LENIENT_CITY_PHRASE}",
    rf"(?i:\bto\b)\s+{_LENIENT_CITY_PHRASE}",
]


def extract_destination_change_trigger_lenient(text: str) -> Optional[str]:
    """Fallback for the change_destination_city intent (see actions.py's
    ActionHandleDestinationChange) — DIET's own trained entity tagging
    on that intent's examples ("actually take me to [Rome](destination_city)
    instead", "change the destination to [Vienna](destination_city)", ...)
    is the PRIMARY extraction path (an untrained/unusual phrasing is
    exactly what this regex fallback is for, same division of labour as
    every other slot in this project — DIET first, lenient regex second).

    Reuses the same 2-word, case-insensitive _LENIENT_CITY_PHRASE (with
    "instead" now in the shared stop-word list, so "Rome instead" is
    never captured as a single 2-word city) rather than inventing a new
    capture shape — most of these change-phrasings still end in a plain
    "to X" the same as an ordinary destination trigger does."""
    return _first_non_date_match(text, _LENIENT_DESTINATION_CHANGE_PATTERNS)


def extract_trip_candidates(text: str) -> dict:
    """For a single combined message like "I am going to go travel from
    Berlin to Rome next week" — tries to pull BOTH cities out of one
    message at once, so the form can skip straight past whichever
    slots this message already answered. Returns whichever of
    "origin"/"destination" it found; a missing key means "not in this
    message", not "not found anywhere" — the caller still asks for it
    normally if absent. Trigger-based only (no bare-message fallback)
    since a combined message, by definition, isn't a direct one-word
    answer to a single specific question.
    """
    result = {}
    origin_match = extract_origin_trigger(text)
    if origin_match:
        result["origin"] = origin_match
    dest_match = extract_destination_trigger(text)
    if dest_match:
        result["destination"] = dest_match
    return result