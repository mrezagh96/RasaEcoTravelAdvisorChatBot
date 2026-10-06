"""
tests/test_content_drift_guards.py — regression tests for the root-cause
fix behind the content-drifting feature (change_destination_city /
ask_trip_to_this_city).

BACKGROUND (see actions.py's _is_content_drift_trigger docstring for the
full mechanism, confirmed via a real trained-model run with DEBUG-level
policy logs): domain.yml's `not_intent` restriction only gates the
DECLARATIVE from_entity/from_text mapping layer. RulePolicy only lets a
condition-scoped "Handle ... during <form>" rule (rules.yml) win once the
form's own execution comes back "rejected" — which only happens when the
currently requested slot ends up with ZERO extraction candidate that
turn. Every `type: custom` slot below bypasses `not_intent` entirely, so
without an explicit intent guard inside extract_<slot> itself, the raw
message text was still handed to validate_<slot> as a "candidate",
validation failed it as a bad date/traveller-count/city/yes-no answer,
and the form silently re-asked instead of ever rejecting — so
action_handle_destination_change / action_start_trip_from_weather_drift
never got a chance to run.

These tests lock in the fix at the unit level (in addition to the real
end-to-end verification already done against the live trained model):
every affected extract_<slot> must return {} (no candidate at all) when
the current message was classified as the relevant content-drift intent.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from actions.actions import ValidateBookConfirmationForm, ValidateTripPlanningForm, ValidateWeatherCheckForm


class FakeDispatcher:
    def __init__(self):
        self.messages = []

    def utter_message(self, **kwargs):
        self.messages.append(kwargs)


class FakeTracker:
    def __init__(self, slots=None, text="", intent="ask_trip_planning", requested_slot=None, entities=None):
        self._slots = dict(slots or {})
        self._slots["requested_slot"] = requested_slot
        self.latest_message = {"text": text, "intent": {"name": intent}, "entities": entities or []}

    def get_slot(self, name):
        return self._slots.get(name)


class TestChangeDestinationCityGuardsTripPlanningForm:
    """change_destination_city must produce a genuinely empty candidate
    for every OTHER trip_planning_form slot that has a `type: custom`
    mapping, wherever it's still unfilled and being asked, so the form
    rejects instead of absorbing the message as a bad answer."""

    def test_origin_city_not_swallowed_when_still_unset(self):
        action = ValidateTripPlanningForm()
        tracker = FakeTracker(
            text="actually, change the destination to Vienna",
            intent="change_destination_city",
            requested_slot="origin_city",
        )
        result = asyncio.run(action.extract_origin_city(FakeDispatcher(), tracker, {}))
        assert result == {}

    def test_origin_city_still_extracted_normally_for_other_intents(self):
        action = ValidateTripPlanningForm()
        tracker = FakeTracker(text="Rome", intent="ask_trip_planning", requested_slot="origin_city")
        result = asyncio.run(action.extract_origin_city(FakeDispatcher(), tracker, {}))
        assert result == {"origin_city": "Rome"}

    def test_travel_date_from_not_swallowed(self):
        action = ValidateTripPlanningForm()
        tracker = FakeTracker(
            text="actually, take me to Vienna instead",
            intent="change_destination_city",
            requested_slot="travel_date_from",
        )
        result = asyncio.run(action.extract_travel_date_from(FakeDispatcher(), tracker, {}))
        assert result == {}

    def test_travel_date_to_not_swallowed(self):
        action = ValidateTripPlanningForm()
        tracker = FakeTracker(
            text="actually, take me to Vienna instead",
            intent="change_destination_city",
            requested_slot="travel_date_to",
        )
        result = asyncio.run(action.extract_travel_date_to(FakeDispatcher(), tracker, {}))
        assert result == {}


class TestChangeDestinationCityGuardsBookConfirmationForm:
    def test_book_confirmation_not_swallowed(self):
        action = ValidateBookConfirmationForm()
        tracker = FakeTracker(
            text="actually, change destination to Vienna",
            intent="change_destination_city",
            requested_slot="book_confirmation",
        )
        result = asyncio.run(action.extract_book_confirmation(FakeDispatcher(), tracker, {}))
        assert result == {}

    def test_book_confirmation_still_extracted_normally_for_other_intents(self):
        action = ValidateBookConfirmationForm()
        tracker = FakeTracker(text="/confirm_booking", intent="confirm_booking", requested_slot="book_confirmation")
        result = asyncio.run(action.extract_book_confirmation(FakeDispatcher(), tracker, {}))
        assert result == {"book_confirmation": "/confirm_booking"}


class TestAskTripToThisCityGuardsWeatherCheckForm:
    def test_weather_day_text_not_swallowed(self):
        action = ValidateWeatherCheckForm()
        tracker = FakeTracker(
            text="I want to go there from Berlin",
            intent="ask_trip_to_this_city",
            requested_slot="weather_day_text",
            entities=[{"entity": "origin_city", "value": "Berlin"}],
        )
        result = asyncio.run(action.extract_weather_day_text(FakeDispatcher(), tracker, {}))
        assert result == {}

    def test_weather_day_text_still_extracted_normally_for_other_intents(self):
        action = ValidateWeatherCheckForm()
        tracker = FakeTracker(text="tomorrow", intent="inform_dates", requested_slot="weather_day_text")
        result = asyncio.run(action.extract_weather_day_text(FakeDispatcher(), tracker, {}))
        assert result == {"weather_day_text": "tomorrow"}
