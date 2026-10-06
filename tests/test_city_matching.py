"""
tests/test_city_matching.py — regex extraction + fuzzy typo-correction
=====================================================================
Run with:  pytest tests/ -v
"""

import asyncio
import copy
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient
from rasa_sdk import Tracker
from rasa_sdk.executor import CollectingDispatcher

from actions import api_clients, city_extractor
from actions.actions import ValidateHotelListForm, ValidateTripPlanningForm, ValidateWeatherCheckForm
from data_services.main import app as data_app

_client = TestClient(data_app)


def _real_match_city(candidate):
    return _client.get("/geocode/suggest", params={"query": candidate}).json()


class FakeDispatcher:
    def __init__(self):
        self.messages = []

    def utter_message(self, **kwargs):
        self.messages.append(kwargs)


class FakeTracker:
    def __init__(self, slots=None, text=""):
        self._slots = slots or {}
        self.latest_message = {"text": text, "intent": {"name": "ask_trip_planning"}, "entities": []}

    def get_slot(self, name):
        return self._slots.get(name)


class TestCityExtractorRegex:
    def test_multi_word_city_not_truncated(self):
        assert city_extractor.extract_destination_candidate("to go to New York") == "New York"
        assert city_extractor.extract_origin_candidate("leaving from Los Angeles") == "Los Angeles"

    def test_trailing_words_excluded(self):
        assert city_extractor.extract_destination_candidate("to New York please") == "New York"
        assert city_extractor.extract_origin_candidate("from Frankfurt next week") == "Frankfurt"

    def test_reversed_order_sentence(self):
        result = city_extractor.extract_trip_candidates("I want to go to Berlin from Dubai")
        assert result == {"origin": "Dubai", "destination": "Berlin"}

    def test_bare_word_fallback(self):
        assert city_extractor.extract_destination_candidate("Rome") == "Rome"
        assert city_extractor.extract_destination_candidate("rome") == "rome"

    def test_combined_message_both_cities(self):
        result = city_extractor.extract_trip_candidates(
            "I am going to go travel from Berlin to Frankfurt next week"
        )
        assert result == {"origin": "Berlin", "destination": "Frankfurt"}


class TestLenientTriggerExtraction:
    """Regression tests for real user reports found while making the
    lenient (any-case) trigger extractors safe to run unconditionally on
    every message, not just while their own slot is being asked."""

    def test_combined_lowercase_from_to_sentence(self):
        assert city_extractor.extract_destination_trigger_lenient(
            "I want to go to frankfurt from berlin"
        ) == "frankfurt"
        assert city_extractor.extract_origin_trigger_lenient(
            "I want to go to frankfurt from berlin"
        ) == "berlin"

    def test_offtopic_sentence_with_nested_trigger_words_yields_nothing(self):
        # "to go to travel" — the sentence's OWN "to go to" trigger phrase
        # leaves "travel" (or, one match down, "go") looking like a
        # one-word city candidate. Neither is a real city; both are
        # themselves trigger/filler words and must be rejected.
        assert city_extractor.extract_destination_trigger_lenient("I want to go to travel") is None

    def test_bare_article_after_trigger_is_never_a_city_candidate(self):
        # BUGFIX (real user report + real execution): "I want to go to a
        # travel from tomorrow to next week from berlin to lisbon" — the
        # "to go to X" pattern's own trigger phrase leaves "a" (the
        # indefinite article right before "travel") looking like a valid
        # one-word city candidate on its own, since "a"/"an"/"the" were
        # not in the stop-word list the way "go"/"travel" already were.
        # That lone-article candidate then reached the live geocoding
        # fallback with no plausibility guard at all (see the matching
        # fix + comment in data_services/geocode.py) and could
        # non-deterministically resolve to an unrelated real place
        # (observed once as "Salta"). No real city name is a bare
        # English article, so a candidate that reduces to JUST one must
        # be rejected, letting the extractor correctly fall through to
        # the NEXT "to" in the sentence and find the real destination.
        #
        # NOTE: this guard only catches a candidate that reduces to
        # nothing but the article itself ("a", "an", "the") — a
        # two-word phrase like "a place" or "an island" still isn't a
        # real city, but the fix here deliberately doesn't try to reject
        # every such phrase (that would need a real gazetteer/NER check,
        # not a word list) since it isn't the bug that was reported. That
        # narrower remaining gap is already caught one layer downstream:
        # match_city() only ever fuzzy-matches such a phrase to a
        # CURATED city with an unconfirmed "Is this what you mean?"
        # prompt (never a silent accept), unlike the bare single-letter
        # case, which is what made the live-geocode path dangerous.
        assert city_extractor.extract_destination_trigger_lenient(
            "I want to go to a travel from tomorrow to next week from berlin to lisbon"
        ) == "lisbon"

    def test_the_hague_is_not_broken_by_the_article_guard(self):
        # The article guard must reject a candidate that reduces to JUST
        # "a"/"an"/"the" on its own — it must NOT reject a real, longer
        # city name that merely starts with one of those words.
        assert city_extractor.extract_destination_trigger_lenient(
            "I want to go to The Hague"
        ) == "The Hague"

    def test_generic_trigger_picks_the_earliest_real_city_not_priority_order(self):
        # BUGFIX (real user report): "what is weather of Tehran at the
        # weekend?" — the fixed trigger-word priority order (in, for, at,
        # of) let "at the weekend" (a date phrase, matched via "at") win
        # over "of Tehran", even though "of Tehran" appears EARLIER in
        # the sentence. Must scan left-to-right and skip date-phrase
        # candidates entirely.
        assert city_extractor.extract_city_trigger_lenient(
            "what is weather of Tehran at the weekend?"
        ) == "Tehran"

    def test_date_ish_second_word_never_gets_captured(self):
        assert city_extractor.extract_city_trigger_lenient("check weather in Rome next Sunday") == "Rome"

    def test_possessive_phrasing_does_not_swallow_a_leading_glue_word(self):
        # "is Berlin's weather" used to be captured as "is Berlin" — the
        # possessive pattern had no trigger word in front of it to
        # anchor where the real city starts, unlike every other pattern.
        assert city_extractor.extract_city_trigger_lenient("what is Berlin's weather today?") == "Berlin"
        assert city_extractor.extract_city_trigger_lenient("what's Tehran's forecast?") == "Tehran"

    def test_multi_word_city_names_still_work_through_all_these_guards(self):
        assert city_extractor.extract_city_trigger_lenient("check weather in Sao Paulo") == "Sao Paulo"
        assert city_extractor.extract_city_trigger_lenient("check weather in new york") == "new york"

    def test_bare_city_weather_compound_noun_with_no_trigger_word_at_all(self):
        # Regression test for a real user report + real debug log:
        # "check Tehran wheather for today" has NO trigger word
        # (in/for/at/of) anywhere near the city, and no possessive
        # "'s" either — just "<City> weather" as a bare compound noun
        # — so neither the generic trigger patterns nor the possessive
        # pattern could ever match it. The real reported typo
        # ("wheather") must resolve exactly like the correct spelling.
        assert city_extractor.extract_city_trigger_lenient("check Tehran wheather for today") == "Tehran"
        assert city_extractor.extract_city_trigger_lenient("check Tehran weather for today") == "Tehran"
        assert city_extractor.extract_city_trigger_lenient("Rome forecast please") == "Rome"
        assert city_extractor.extract_city_trigger_lenient("New York weather today") == "New York"

    def test_bare_city_weather_pattern_does_not_swallow_a_leading_glue_word(self):
        # "check weather in Rome" — "check" sits directly in front of
        # "weather" here, so the bare compound-noun pattern must NOT
        # capture "check" as if it were the city (the lead-word
        # stoplist it shares with the possessive pattern already
        # blocks this; the "in Rome" generic pattern is what's meant
        # to win instead).
        assert city_extractor.extract_city_trigger_lenient("check weather in Rome") == "Rome"
        assert city_extractor.extract_city_trigger_lenient("check the weather in Rome") == "Rome"


class TestFuzzyGeocodeMatch:
    def test_exact_match(self):
        r = _client.get("/geocode/suggest", params={"query": "Berlin"}).json()
        assert r["is_exact"] is True
        assert r["matched_key"] == "berlin"

    def test_typo_is_fuzzy_not_exact(self):
        r = _client.get("/geocode/suggest", params={"query": "Berln"}).json()
        assert r["found"] is True
        assert r["is_exact"] is False
        assert r["matched_name"] == "Berlin"

    def test_accent_variation_counts_as_exact(self):
        r = _client.get("/geocode/suggest", params={"query": "frankfürt"}).json()
        assert r["is_exact"] is True

    def test_missing_space_typo(self):
        r = _client.get("/geocode/suggest", params={"query": "newyork"}).json()
        assert r["is_exact"] is False
        assert r["matched_name"] == "New York"

    def test_nonsense_not_found(self):
        r = _client.get("/geocode/suggest", params={"query": "Xyzznotarealplace123"}).json()
        assert r["found"] is False

    def test_single_letter_query_is_rejected_before_live_geocode(self):
        # BUGFIX (real user report + real execution): a single-character
        # query ("a", captured upstream from "...to go to a travel...")
        # used to fall straight through to the live Nominatim fallback,
        # which has no minimum-length floor of its own and can
        # non-deterministically resolve a garbage 1-letter query to a
        # real but totally unrelated place (observed once as "Salta").
        # No real city name is shorter than 2 characters, so this must
        # be rejected up front, before ever reaching the live lookup.
        r = _client.get("/geocode/suggest", params={"query": "a"}).json()
        assert r["found"] is False
        assert r["source"] == "not_found"

    def test_two_letter_query_is_not_rejected_by_the_length_guard(self):
        # The length guard must only reject queries shorter than 2
        # characters — it must not block a genuinely short real city
        # name search from reaching curated/fuzzy matching normally.
        r = _client.get("/geocode/suggest", params={"query": "Berlin"}).json()
        assert r["found"] is True
        assert r["is_exact"] is True


class TestFormIntegration:
    def test_typo_triggers_confirmation_with_yes_button(self):
        action = ValidateTripPlanningForm()
        dispatcher = FakeDispatcher()
        with patch.object(api_clients, "match_city", side_effect=_real_match_city):
            result = action.validate_origin_city("Berln", dispatcher, FakeTracker(), {})
        assert result == {
            "origin_city": None,
            "pending_city_field": "origin_city",
            "pending_city_suggestion": "Berlin",
        }
        assert dispatcher.messages[0]["buttons"][0] == {"title": "Yes", "payload": "/affirm_city_suggestion"}

    def test_exact_match_accepted_with_no_message(self):
        action = ValidateTripPlanningForm()
        dispatcher = FakeDispatcher()
        with patch.object(api_clients, "match_city", side_effect=_real_match_city):
            result = action.validate_origin_city("Berlin", dispatcher, FakeTracker(), {})
        assert result == {"origin_city": "Berlin"}
        assert dispatcher.messages == []

    def test_payload_confirmation_click_is_not_re_parsed_by_regex(self):
        action = ValidateTripPlanningForm()
        tracker = FakeTracker(slots={}, text="/affirm_city_suggestion")
        result = asyncio.run(action.extract_origin_city(FakeDispatcher(), tracker, {}))
        assert result == {}

    def test_already_filled_slot_is_never_overwritten(self):
        action = ValidateTripPlanningForm()
        tracker = FakeTracker(slots={"origin_city": "Paris"}, text="I want to go to Rome from Madrid")
        result = asyncio.run(action.extract_origin_city(FakeDispatcher(), tracker, {}))
        assert result == {}

    def test_combined_message_fills_both_city_slots_in_one_turn(self):
        action = ValidateTripPlanningForm()
        tracker = FakeTracker(slots={}, text="I want to go to Berlin from Dubai")
        origin = asyncio.run(action.extract_origin_city(FakeDispatcher(), tracker, {}))
        destination = asyncio.run(action.extract_destination_city(FakeDispatcher(), tracker, {}))
        assert origin == {"origin_city": "Dubai"}
        assert destination == {"destination_city": "Berlin"}

    def test_origin_same_as_destination_is_rejected(self):
        # Q1/Q2 order is destination-then-origin in the current
        # architecture, so this check now lives in validate_origin_city
        # (checking against the already-filled destination_city), not
        # the other way around.
        action = ValidateTripPlanningForm()
        dispatcher = FakeDispatcher()
        tracker = FakeTracker(slots={"destination_city": "Berlin"})
        with patch.object(api_clients, "match_city", side_effect=_real_match_city):
            result = action.validate_origin_city("Berlin", dispatcher, tracker, {})
        assert result == {"origin_city": None}

    def test_yes_click_resolves_directly_from_pending_suggestion(self):
        # Regression test for a real, reproduced bug: the "Yes" button's
        # raw payload text ("/affirm_city_suggestion") was reaching
        # validate_origin_city as the literal candidate — because this
        # slot's own from_text mapping grabs whatever raw text arrived,
        # regardless of intent, before any rule's prediction matters —
        # and got wrongly treated as a city name to fuzzy-match, instead
        # of being resolved from pending_city_suggestion.
        action = ValidateTripPlanningForm()
        dispatcher = FakeDispatcher()
        tracker = FakeTracker(slots={"pending_city_field": "origin_city", "pending_city_suggestion": "Berlin"})
        result = action.validate_origin_city("/affirm_city_suggestion", dispatcher, tracker, {})
        assert result == {"origin_city": "Berlin", "pending_city_field": None, "pending_city_suggestion": None}
        assert dispatcher.messages == []  # no "I couldn't find a place called '/affirm_city_suggestion'"

    def test_yes_click_resolution_is_correct_across_two_independent_calls(self):
        # A genuine Rasa duplicate-validate-action-invocation (see
        # _is_duplicate_form_run's docstring) sends BOTH calls the SAME
        # pre-response tracker snapshot — Core hasn't applied either
        # response's events yet when it decides to call a second time,
        # since that decision is made from ITS OWN state, independent of
        # what the action server returns. Two independent FakeTracker
        # instances (each un-mutated by the OTHER call, matching that
        # reality) must both resolve identically and correctly.
        action = ValidateTripPlanningForm()
        tracker_a = FakeTracker(slots={"pending_city_field": "origin_city", "pending_city_suggestion": "Berlin"})
        tracker_b = FakeTracker(slots={"pending_city_field": "origin_city", "pending_city_suggestion": "Berlin"})
        first = action.validate_origin_city("/affirm_city_suggestion", FakeDispatcher(), tracker_a, {})
        second = action.validate_origin_city("/affirm_city_suggestion", FakeDispatcher(), tracker_b, {})
        expected = {"origin_city": "Berlin", "pending_city_field": None, "pending_city_suggestion": None}
        assert first == expected
        assert second == expected

    def test_yes_click_with_nothing_pending_falls_through_to_normal_not_found(self):
        # A stray/duplicate click with no pending suggestion shouldn't
        # silently do nothing — it should behave like any other
        # unrecognisable candidate (ask again), not leave the user stuck.
        # The message shown must NEVER echo the raw payload text back.
        action = ValidateTripPlanningForm()
        dispatcher = FakeDispatcher()
        with patch.object(api_clients, "match_city", side_effect=_real_match_city):
            result = action.validate_origin_city("/affirm_city_suggestion", dispatcher, FakeTracker(), {})
        assert result == {"origin_city": None}
        assert "/affirm_city_suggestion" not in dispatcher.messages[0]["text"]

    def test_yes_click_only_resolves_for_the_slot_it_was_pending_for(self):
        # If the pending confirmation was for destination_city, a stray
        # "/affirm_city_suggestion" arriving while validating origin_city
        # must NOT resolve it — that would silently misassign the
        # suggestion to the wrong slot.
        action = ValidateTripPlanningForm()
        dispatcher = FakeDispatcher()
        tracker = FakeTracker(slots={"pending_city_field": "destination_city", "pending_city_suggestion": "Berlin"})
        with patch.object(api_clients, "match_city", side_effect=_real_match_city):
            result = action.validate_origin_city("/affirm_city_suggestion", dispatcher, tracker, {})
        assert result == {"origin_city": None}


def _real_tracker(sender_id, slots, text, intent, active_loop, entities=None):
    """Builds a genuine rasa_sdk.Tracker (not the hand-rolled FakeTracker
    above) — needed for the tests below, which exercise the REAL
    extract_<slot> -> tracker.add_slots -> slots_to_validate ->
    validate_<slot> wiring end to end, not just validate_<slot> in
    isolation. That wiring (specifically rasa_sdk's own
    Tracker.slots_to_validate(), which only ever validates a slot that
    was actually SlotSet on the tracker's trailing events) is exactly
    what the real, reported bug lived in — calling validate_<slot>
    directly (as every test above does) can never catch a bug in
    whether it gets CALLED at all.

    `entities` defaults to `[]` (a plain typed/free-text message), but
    can be given the same shape Rasa Core's own RegexMessageHandler
    produces for a structured `/intent{"slot": "value"}` button payload
    (confirmed via a real debug log: `[{"entity": "weather_day_text",
    "value": "tomorrow", "start": ..., "end": ..., "extractor":
    "RegexMessageHandler"}]`) — needed by the date/city quick-pick
    button regression tests below."""
    return Tracker.from_dict(
        {
            "sender_id": sender_id,
            "slots": dict(slots),
            "latest_message": {
                "text": text,
                "intent": {"name": intent, "confidence": 1.0},
                "intent_ranking": [{"name": intent, "confidence": 1.0}],
                "entities": entities if entities is not None else [],
            },
            "events": [
                {"event": "action", "name": "utter_ask_confirm_city"},
                {"event": "user", "text": text, "parse_data": {}},
            ],
            "paused": False,
            "followup_action": None,
            "active_loop": {"name": active_loop},
            "latest_action_name": "action_listen",
        }
    )


async def _extract_then_validate(action, tracker, domain):
    """Mirrors exactly what rasa_sdk's ValidationAction.run() does (see
    forms.py): extraction events are computed AND applied to the
    tracker first (so slots_to_validate() can see them), then
    validation runs against whatever actually landed there — never
    against a hand-picked candidate the test chose itself."""
    dispatcher = CollectingDispatcher()
    extraction_events = await action.get_extraction_events(dispatcher, tracker, domain)
    tracker.add_slots(extraction_events)
    validation_events = await action.get_validation_events(dispatcher, tracker, domain)
    return validation_events, dispatcher


class TestYesClickFullExtractionPipeline:
    """Regression tests for a real, reported, reproduced-via-debug-log
    bug: clicking "Yes" (or typing "yes") to confirm a fuzzy-matched
    city suggestion made Rasa Core log "Execution of '...' was
    rejected. Setting its confidence to 0.0 in all predictions." and
    fall through to action_smart_fallback, instead of resolving the
    slot to the suggested city.

    Root cause: weather_city/origin_city/destination_city/hotel_list_city
    all use a `type: custom` domain.yml mapping (extraction delegated
    entirely to their own extract_<slot> method — see domain.yml's
    comments on these slots), and every one of those extract_<slot>
    methods unconditionally returned `{}` for any message starting with
    "/" — including the "Yes" button's own "/affirm_city_suggestion"
    payload. Since `type: custom` means there is no OTHER extraction
    path (no from_text/from_entity fallback Core can use instead), that
    early return meant the slot was NEVER extracted at all on the
    Yes-click turn — so validate_<slot> (where _match_city_or_ask's
    "Yes" resolution logic actually lives) was never even called, Core
    saw the form produce zero progress that turn, and rejected it.

    The existing TestFormIntegration tests
    (test_yes_click_resolves_directly_from_pending_suggestion etc.)
    all call validate_origin_city directly with a hand-picked candidate
    string — which is exactly why they kept passing throughout while
    this bug shipped: they never exercise whether extract_<slot> lets
    that candidate through in the first place. These tests go through
    the real extraction step too, which is where the bug actually was.
    """

    def test_weather_city_yes_click_resolves_through_full_pipeline(self):
        action = ValidateWeatherCheckForm()
        tracker = _real_tracker(
            "yc-weather-1",
            slots={
                "weather_city": None,
                "requested_slot": "weather_city",
                "pending_city_field": "weather_city",
                "pending_city_suggestion": "Tehran",
            },
            text="/affirm_city_suggestion",
            intent="affirm_city_suggestion",
            active_loop="weather_check_form",
        )
        domain = {"forms": {"weather_check_form": {"required_slots": ["weather_city", "weather_day_text"]}}}
        events, dispatcher = asyncio.run(_extract_then_validate(action, tracker, domain))
        by_name = {e["name"]: e["value"] for e in events}
        assert by_name.get("weather_city") == "Tehran"
        assert by_name.get("pending_city_field") is None
        assert by_name.get("pending_city_suggestion") is None
        assert dispatcher.messages == []  # no spurious "couldn't find" text

    def test_weather_city_typed_yes_resolves_through_full_pipeline(self):
        action = ValidateWeatherCheckForm()
        tracker = _real_tracker(
            "yc-weather-2",
            slots={
                "weather_city": None,
                "requested_slot": "weather_city",
                "pending_city_field": "weather_city",
                "pending_city_suggestion": "Tehran",
            },
            text="yes",
            intent="affirm",
            active_loop="weather_check_form",
        )
        domain = {"forms": {"weather_check_form": {"required_slots": ["weather_city", "weather_day_text"]}}}
        events, _ = asyncio.run(_extract_then_validate(action, tracker, domain))
        by_name = {e["name"]: e["value"] for e in events}
        assert by_name.get("weather_city") == "Tehran"

    def test_origin_city_yes_click_resolves_through_full_pipeline(self):
        action = ValidateTripPlanningForm()
        tracker = _real_tracker(
            "yc-origin-1",
            slots={
                "origin_city": None,
                "destination_city": "Berlin",
                "requested_slot": "origin_city",
                "pending_city_field": "origin_city",
                "pending_city_suggestion": "Dubai",
            },
            text="/affirm_city_suggestion",
            intent="affirm_city_suggestion",
            active_loop="trip_planning_form",
        )
        domain = {"forms": {"trip_planning_form": {"required_slots": ["destination_city", "origin_city"]}}}
        events, _ = asyncio.run(_extract_then_validate(action, tracker, domain))
        by_name = {e["name"]: e["value"] for e in events}
        assert by_name.get("origin_city") == "Dubai"

    def test_destination_city_yes_click_resolves_through_full_pipeline(self):
        action = ValidateTripPlanningForm()
        tracker = _real_tracker(
            "yc-destination-1",
            slots={
                "destination_city": None,
                "requested_slot": "destination_city",
                "pending_city_field": "destination_city",
                "pending_city_suggestion": "Frankfurt",
            },
            text="/affirm_city_suggestion",
            intent="affirm_city_suggestion",
            active_loop="trip_planning_form",
        )
        domain = {"forms": {"trip_planning_form": {"required_slots": ["destination_city", "origin_city"]}}}
        events, _ = asyncio.run(_extract_then_validate(action, tracker, domain))
        by_name = {e["name"]: e["value"] for e in events}
        assert by_name.get("destination_city") == "Frankfurt"

    def test_hotel_list_city_yes_click_resolves_through_full_pipeline(self):
        action = ValidateHotelListForm()
        tracker = _real_tracker(
            "yc-hotel-1",
            slots={
                "hotel_list_city": None,
                "requested_slot": "hotel_list_city",
                "pending_city_field": "hotel_list_city",
                "pending_city_suggestion": "New York",
            },
            text="/affirm_city_suggestion",
            intent="affirm_city_suggestion",
            active_loop="hotel_list_form",
        )
        domain = {"forms": {"hotel_list_form": {"required_slots": ["hotel_list_city"]}}}
        events, _ = asyncio.run(_extract_then_validate(action, tracker, domain))
        by_name = {e["name"]: e["value"] for e in events}
        assert by_name.get("hotel_list_city") == "New York"

    def test_stray_slash_payload_with_nothing_pending_still_extracts_nothing(self):
        # No pending_city_field at all — a stray/unrelated "/something"
        # payload must still be extracted as NOTHING, exactly as before
        # this fix (this is the guard the fix must not weaken).
        action = ValidateWeatherCheckForm()
        tracker = _real_tracker(
            "yc-weather-stray",
            slots={"weather_city": None, "requested_slot": "weather_city"},
            text="/see_cities_list",
            intent="see_cities_list",
            active_loop="weather_check_form",
        )
        domain = {"forms": {"weather_check_form": {"required_slots": ["weather_city", "weather_day_text"]}}}
        events, _ = asyncio.run(_extract_then_validate(action, tracker, domain))
        assert events == []  # nothing extracted, nothing validated

    def test_pending_confirmation_for_a_different_slot_is_not_hijacked(self):
        # A pending confirmation for destination_city must not make
        # weather_city's own extractor swallow an unrelated "/"-prefixed
        # payload — the bypass is scoped to THIS slot's own pending field.
        action = ValidateWeatherCheckForm()
        tracker = _real_tracker(
            "yc-weather-wrong-slot",
            slots={
                "weather_city": None,
                "requested_slot": "weather_city",
                "pending_city_field": "destination_city",
                "pending_city_suggestion": "Berlin",
            },
            text="/some_other_payload",
            intent="some_other_intent",
            active_loop="weather_check_form",
        )
        domain = {"forms": {"weather_check_form": {"required_slots": ["weather_city", "weather_day_text"]}}}
        events, _ = asyncio.run(_extract_then_validate(action, tracker, domain))
        assert events == []


class TestStructuredButtonPayloadFullExtractionPipeline:
    """Regression tests for a second, real, reported, debug-log-confirmed
    bug - the same rejection as TestYesClickFullExtractionPipeline above,
    but triggered by a DIFFERENT kind of "/"-prefixed payload: a
    quick-reply button whose payload carries the slot's own value
    directly (e.g. `/inform_dates{"weather_day_text": "tomorrow"}`),
    rather than a city-confirmation "Yes" click.

    Reported bug: clicking the "Tomorrow" button after picking a city in
    Weather Checking printed "Sorry, that doesn't seem to answer what I
    just asked" instead of resolving the day - confirmed via a real
    debug log and reproduced with a real trained model + action server
    (see repro_button_bug.py). Auditing every other quick-pick button in
    the project the same way (per the user's own request, "check the
    rest yourself") found the EXACT SAME bug also breaking the
    destination_city/origin_city city-grid buttons at the very start of
    Trip Assistant, and the travel_date_from date buttons - all of
    which are `type: custom` domain.yml mappings with no other
    extraction path, exactly like the Yes-click bug's slots.

    Root cause: Rasa Core's own RegexMessageHandler parses a structured
    payload's JSON body into `tracker.latest_message`'s entities BEFORE
    the action server ever runs, but every extract_<slot> method
    unconditionally returned `{}` for ANY "/"-prefixed text without ever
    looking at those entities - see _structured_payload_value's
    docstring in actions.py for the full explanation."""

    def test_weather_day_text_button_click_resolves_through_full_pipeline(self):
        action = ValidateWeatherCheckForm()
        tracker = _real_tracker(
            "btn-weather-day-1",
            slots={"weather_city": "Berlin", "weather_day_text": None, "requested_slot": "weather_day_text"},
            text='/inform_dates{"weather_day_text": "tomorrow"}',
            intent="inform_dates",
            active_loop="weather_check_form",
            entities=[{"entity": "weather_day_text", "value": "tomorrow", "start": 13, "end": 45, "extractor": "RegexMessageHandler"}],
        )
        domain = {"forms": {"weather_check_form": {"required_slots": ["weather_city", "weather_day_text"]}}}
        events, dispatcher = asyncio.run(_extract_then_validate(action, tracker, domain))
        by_name = {e["name"]: e["value"] for e in events}
        assert by_name.get("weather_day_text") == "tomorrow"
        assert dispatcher.messages == []  # no spurious "couldn't work out a date" text

    def test_weather_day_text_next_week_button_click_resolves_through_full_pipeline(self):
        # The specific button ("Next week, this day") whose payload was
        # attached alongside a stray, unexplained KeyError traceback
        # fragment in the user's debug log - re-verified via real
        # trained-model + action-server execution (repro_button_bug.py)
        # that this exact payload now resolves cleanly with no exception
        # anywhere in the action server's own log, same as every other
        # date button.
        action = ValidateWeatherCheckForm()
        tracker = _real_tracker(
            "btn-weather-day-2",
            slots={"weather_city": "Berlin", "weather_day_text": None, "requested_slot": "weather_day_text"},
            text='/inform_dates{"weather_day_text": "next week"}',
            intent="inform_dates",
            active_loop="weather_check_form",
            entities=[{"entity": "weather_day_text", "value": "next week", "start": 13, "end": 46, "extractor": "RegexMessageHandler"}],
        )
        domain = {"forms": {"weather_check_form": {"required_slots": ["weather_city", "weather_day_text"]}}}
        events, _ = asyncio.run(_extract_then_validate(action, tracker, domain))
        by_name = {e["name"]: e["value"] for e in events}
        assert by_name.get("weather_day_text") == "next week"

    def test_destination_city_grid_button_click_resolves_through_full_pipeline(self):
        action = ValidateTripPlanningForm()
        tracker = _real_tracker(
            "btn-destination-1",
            slots={"destination_city": None, "requested_slot": "destination_city"},
            text='/ask_trip_planning{"destination_city": "London"}',
            intent="ask_trip_planning",
            active_loop="trip_planning_form",
            entities=[{"entity": "destination_city", "value": "London", "start": 16, "end": 49, "extractor": "RegexMessageHandler"}],
        )
        domain = {"forms": {"trip_planning_form": {"required_slots": ["destination_city", "origin_city"]}}}
        events, _ = asyncio.run(_extract_then_validate(action, tracker, domain))
        by_name = {e["name"]: e["value"] for e in events}
        assert by_name.get("destination_city") == "London"

    def test_origin_city_grid_button_click_resolves_through_full_pipeline(self):
        action = ValidateTripPlanningForm()
        tracker = _real_tracker(
            "btn-origin-1",
            slots={"destination_city": "Paris", "origin_city": None, "requested_slot": "origin_city"},
            text='/ask_trip_planning{"origin_city": "Rome"}',
            intent="ask_trip_planning",
            active_loop="trip_planning_form",
            entities=[{"entity": "origin_city", "value": "Rome", "start": 16, "end": 41, "extractor": "RegexMessageHandler"}],
        )
        domain = {"forms": {"trip_planning_form": {"required_slots": ["destination_city", "origin_city"]}}}
        events, _ = asyncio.run(_extract_then_validate(action, tracker, domain))
        by_name = {e["name"]: e["value"] for e in events}
        assert by_name.get("origin_city") == "Rome"

    def test_travel_date_from_button_click_resolves_through_full_pipeline(self):
        action = ValidateTripPlanningForm()
        tracker = _real_tracker(
            "btn-date-from-1",
            slots={
                "destination_city": "Paris", "origin_city": "Rome",
                "travel_date_from": None, "requested_slot": "travel_date_from",
            },
            text='/inform_dates{"travel_date_from": "tomorrow"}',
            intent="inform_dates",
            active_loop="trip_planning_form",
            entities=[{"entity": "travel_date_from", "value": "tomorrow", "start": 13, "end": 45, "extractor": "RegexMessageHandler"}],
        )
        domain = {"forms": {"trip_planning_form": {"required_slots": ["destination_city", "origin_city", "travel_date_from", "travel_date_to"]}}}
        events, _ = asyncio.run(_extract_then_validate(action, tracker, domain))
        by_name = {e["name"]: e["value"] for e in events}
        assert by_name.get("travel_date_from") == "tomorrow"

    def test_stray_slash_payload_without_matching_entity_still_extracts_nothing(self):
        # A "/"-prefixed payload that carries NO entity named after this
        # slot (e.g. a plain nav-menu payload, or a structured payload
        # for a completely different slot) must still be extracted as
        # NOTHING - this is the guard the fix must not weaken.
        action = ValidateWeatherCheckForm()
        tracker = _real_tracker(
            "btn-weather-day-stray",
            slots={"weather_city": "Berlin", "weather_day_text": None, "requested_slot": "weather_day_text"},
            text='/ask_trip_planning{"destination_city": "London"}',
            intent="ask_trip_planning",
            active_loop="weather_check_form",
            entities=[{"entity": "destination_city", "value": "London", "start": 16, "end": 49, "extractor": "RegexMessageHandler"}],
        )
        domain = {"forms": {"weather_check_form": {"required_slots": ["weather_city", "weather_day_text"]}}}
        events, _ = asyncio.run(_extract_then_validate(action, tracker, domain))
        assert events == []