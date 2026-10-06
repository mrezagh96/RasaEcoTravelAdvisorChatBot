"""
tests/test_form_bugs.py — regression tests for two bugs found during
live testing on 2026-09 (see chat history):

  1. A nonsense/off-topic message (e.g. "I want to go to travel") was
     being treated as a city-name candidate whenever the whole-message
     fallback fired, regardless of whether the bot was even asking for
     a city right now.
  2. Rasa has a documented quirk (RasaHQ/rasa#11595, rasa-sdk#100)
     where a form's validate action can be invoked twice for one
     incoming message when a slot fails validation, duplicating every
     dispatcher.utter_message() sent and making the bot appear to skip
     straight to the next question without waiting for the user.

Run with:  pytest tests/ -v
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from actions.actions import ValidateTripPlanningForm, _is_duplicate_form_run


class FakeDispatcher:
    def __init__(self):
        self.messages = []

    def utter_message(self, **kwargs):
        self.messages.append(kwargs)


class FakeTracker:
    def __init__(self, slots=None, text="", requested_slot=None, events=None, sender_id="test-conv"):
        self._slots = dict(slots or {})
        self._slots["requested_slot"] = requested_slot
        self.latest_message = {"text": text, "intent": {"name": "ask_trip_planning"}, "entities": []}
        self.events = events if events is not None else [{}] * 5
        self.sender_id = sender_id

    def get_slot(self, name):
        return self._slots.get(name)


class TestNonsenseNotTreatedAsCity:
    def test_off_topic_sentence_ignored_when_not_the_requested_slot(self):
        action = ValidateTripPlanningForm()
        tracker = FakeTracker(text="I want to go to travel", requested_slot=None)
        result = asyncio.run(action.extract_destination_city(FakeDispatcher(), tracker, {}))
        assert result == {}

    def test_bare_answer_still_works_when_it_is_the_requested_slot(self):
        action = ValidateTripPlanningForm()
        tracker = FakeTracker(text="rome", requested_slot="destination_city")
        result = asyncio.run(action.extract_destination_city(FakeDispatcher(), tracker, {}))
        assert result == {"destination_city": "rome"}

    def test_trigger_phrase_works_regardless_of_requested_slot(self):
        action = ValidateTripPlanningForm()
        tracker = FakeTracker(text="I want to go to Berlin from Dubai", requested_slot="num_travelers")
        origin = asyncio.run(action.extract_origin_city(FakeDispatcher(), tracker, {}))
        destination = asyncio.run(action.extract_destination_city(FakeDispatcher(), tracker, {}))
        assert origin == {"origin_city": "Dubai"}
        assert destination == {"destination_city": "Berlin"}


class TestDuplicateFormRunGuard:
    def test_second_call_same_turn_is_flagged_duplicate(self):
        tracker = FakeTracker(text="Berln", events=[{}] * 7, sender_id="dedup-a")
        assert _is_duplicate_form_run(tracker) is False
        assert _is_duplicate_form_run(tracker) is True

    def test_new_turn_with_different_event_count_is_not_duplicate(self):
        tracker_a = FakeTracker(text="Rome", events=[{}] * 3, sender_id="dedup-b")
        tracker_b = FakeTracker(text="Rome", events=[{}] * 5, sender_id="dedup-b")
        assert _is_duplicate_form_run(tracker_a) is False
        assert _is_duplicate_form_run(tracker_b) is False

    def test_different_conversations_never_collide(self):
        tracker_a = FakeTracker(text="Rome", events=[{}] * 4, sender_id="dedup-c1")
        tracker_b = FakeTracker(text="Rome", events=[{}] * 4, sender_id="dedup-c2")
        assert _is_duplicate_form_run(tracker_a) is False
        assert _is_duplicate_form_run(tracker_b) is False
