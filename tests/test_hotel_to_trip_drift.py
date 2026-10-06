"""
tests/test_hotel_to_trip_drift.py — "make a trip to there" must work after
a hotel list exactly like it does after a weather check.

Real bug this locks in (from a real conversation log): after the hotel list
for Tehran finished, hotel_list_city was reset and nothing remembered it,
so ActionStartTripFromWeatherDrift found no city, asked "which city?", and
the user's answer was then bounced by the awaiting_finish reminder.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from actions import actions
from actions.actions import (
    ActionAnswerHotelList,
    ActionAnswerWeatherCheck,
    ActionStartTripFromWeatherDrift,
    ValidateTripPlanningForm,
    _named_destination,
)


class FakeDispatcher:
    def __init__(self):
        self.messages = []

    def utter_message(self, **kwargs):
        self.messages.append(kwargs)


class FakeTracker:
    def __init__(self, slots=None, text="make a trip to there", entities=None):
        self._slots = dict(slots or {})
        self.latest_message = {
            "text": text,
            "intent": {"name": "ask_trip_to_this_city"},
            "entities": entities or [],
        }

    def get_slot(self, name):
        return self._slots.get(name)


def slot_values(events):
    return {e["name"]: e["value"] for e in events if e.get("event") == "slot"}


def has_followup(events, action_name):
    return any(e.get("event") == "followup" and e.get("name") == action_name for e in events)


class TestHotelListRemembersCity:
    def test_city_mirrored_into_last_checked_city(self, monkeypatch):
        monkeypatch.setattr(actions.api_clients, "geocode", lambda city: {"lat": 35.7, "lon": 51.4})
        monkeypatch.setattr(
            actions.api_clients, "find_hotels", lambda lat, lon, city: [{"name": "Hotel Alborz", "stars": None, "eco_tag": True}]
        )
        tracker = FakeTracker(slots={"hotel_list_city": "Tehran"})
        events = ActionAnswerHotelList().run(FakeDispatcher(), tracker, {})
        values = slot_values(events)
        assert values["last_checked_city"] == "Tehran"
        assert values["hotel_list_city"] is None
        assert values["awaiting_finish"] is True

    def test_city_still_remembered_when_no_hotels_found(self, monkeypatch):
        monkeypatch.setattr(actions.api_clients, "geocode", lambda city: {"lat": 35.7, "lon": 51.4})
        monkeypatch.setattr(actions.api_clients, "find_hotels", lambda lat, lon, city: [])
        tracker = FakeTracker(slots={"hotel_list_city": "Tehran"})
        events = ActionAnswerHotelList().run(FakeDispatcher(), tracker, {})
        assert slot_values(events)["last_checked_city"] == "Tehran"

    def test_unknown_city_is_not_remembered(self, monkeypatch):
        monkeypatch.setattr(actions.api_clients, "geocode", lambda city: None)
        tracker = FakeTracker(slots={"hotel_list_city": "Nowhereville"})
        events = ActionAnswerHotelList().run(FakeDispatcher(), tracker, {})
        assert "last_checked_city" not in slot_values(events)


class TestWeatherCheckWritesSameSlot:
    def test_weather_city_mirrored_into_last_checked_city(self, monkeypatch):
        monkeypatch.setattr(actions.api_clients, "geocode", lambda city: {"lat": 41.9, "lon": 12.5})
        monkeypatch.setattr(
            actions.api_clients,
            "get_weather_forecast",
            lambda lat, lon, d, n: {
                "days": [{"date": d, "temperature_max_c": 20, "precipitation_mm": 0, "advice": "nice", "source": "live"}]
            },
        )
        tracker = FakeTracker(slots={"weather_city": "Rome", "weather_day_text": "today"})
        events = ActionAnswerWeatherCheck().run(FakeDispatcher(), tracker, {})
        assert slot_values(events)["last_checked_city"] == "Rome"


class TestDriftActionUsesRememberedCity:
    def test_trip_starts_for_hotel_city(self):
        tracker = FakeTracker(slots={"last_checked_city": "Tehran", "awaiting_finish": True})
        dispatcher = FakeDispatcher()
        events = ActionStartTripFromWeatherDrift().run(dispatcher, tracker, {})
        values = slot_values(events)
        assert values["destination_city"] == "Tehran"
        assert values["awaiting_finish"] is False
        assert has_followup(events, "trip_planning_form")
        assert "Tehran" in dispatcher.messages[0]["text"]

    def test_active_weather_city_wins_over_older_check(self):
        tracker = FakeTracker(slots={"weather_city": "Berlin", "last_checked_city": "Tehran"})
        events = ActionStartTripFromWeatherDrift().run(FakeDispatcher(), tracker, {})
        assert slot_values(events)["destination_city"] == "Berlin"

    def test_origin_given_inline_is_still_picked_up(self):
        tracker = FakeTracker(
            slots={"last_checked_city": "Tehran"},
            text="I want to go there from Berlin",
            entities=[{"entity": "origin_city", "value": "Berlin"}],
        )
        events = ActionStartTripFromWeatherDrift().run(FakeDispatcher(), tracker, {})
        values = slot_values(events)
        assert values["destination_city"] == "Tehran"
        assert values["origin_city"] == "Berlin"

    def test_no_remembered_city_hands_over_to_trip_form(self):
        # Nothing to carry over: the form itself asks Q1, so a bare city
        # name is accepted whatever NLU makes of it.
        tracker = FakeTracker(slots={"awaiting_finish": True})
        dispatcher = FakeDispatcher()
        events = ActionStartTripFromWeatherDrift().run(dispatcher, tracker, {})
        assert slot_values(events)["awaiting_finish"] is False
        assert "destination_city" not in slot_values(events)
        assert has_followup(events, "trip_planning_form")

    def test_city_named_in_message_beats_remembered_city(self):
        # NLU can mislabel a full trip sentence as "there"; a destination
        # named in the text must win over the remembered city.
        tracker = FakeTracker(
            slots={"last_checked_city": "Tehran"},
            text="I wan to go to a trip from Berlin to Paris from this weekend to the next weekend",
        )
        events = ActionStartTripFromWeatherDrift().run(FakeDispatcher(), tracker, {})
        assert "destination_city" not in slot_values(events)
        assert has_followup(events, "trip_planning_form")


class TestNamedDestination:
    def test_capitalised_city_after_to(self):
        assert _named_destination("a trip from Berlin to Paris from this weekend") == "Paris"

    def test_lowercase_city_needs_to_be_a_real_city(self, monkeypatch):
        monkeypatch.setattr(actions.api_clients, "match_city", lambda c: {"found": True, "matched_name": "Paris"})
        assert _named_destination("i want to go to paris from berlin") == "paris"

    def test_lowercase_non_city_is_ignored(self, monkeypatch):
        monkeypatch.setattr(actions.api_clients, "match_city", lambda c: {"found": False})
        assert _named_destination("plan a trip to a trip") is None

    def test_pronouns_are_not_cities(self):
        assert _named_destination("make a trip to there") is None
        assert _named_destination("plan a trip to that city") is None


class TestTripFormIgnoresPlacePronouns:
    def test_there_is_not_taken_as_destination(self):
        tracker = FakeTracker(text="make a trip to there")
        result = asyncio.run(ValidateTripPlanningForm().extract_destination_city(FakeDispatcher(), tracker, {}))
        assert result == {}