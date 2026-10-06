"""
tests/test_form_required_slots.py — unit test for the adaptive
`priority_focus` question in trip_planning_form.

GAP FOUND while building the Section-5 "Testing" suite (assignment
requirement: "Extend test stories to cover variations in ... sustainability
preferences"): ValidateTripPlanningForm.required_slots() (actions/actions.py)
decides, in pure Python, whether to ask the extra `priority_focus` question
at all — it's only asked when `sustainability_level == "high"`, and is
REMOVED from the slot list otherwise. Nothing in the whole test suite
(pytest or Rasa's own `rasa test core`) exercised this branch directly
before this file: `rasa test core` only evaluates POLICY action
predictions against a fixed, pre-declared event sequence (see
tests/test_stories.yml's own module docstring for why), so it cannot catch
a regression in this method's own internal branching logic — only a direct
unit test of required_slots() itself can. This file is that direct test.

Run with:  pytest tests/ -v
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from actions.actions import ValidateTripPlanningForm

DOMAIN_SLOTS = [
    "destination_city",
    "origin_city",
    "travel_date_from",
    "travel_date_to",
    "num_travelers",
    "budget_amount",
    "sustainability_level",
    "priority_focus",
]


class FakeDispatcher:
    def __init__(self):
        self.messages = []

    def utter_message(self, **kwargs):
        self.messages.append(kwargs)


class FakeTracker:
    def __init__(self, sustainability_level=None):
        self._slots = {"sustainability_level": sustainability_level}

    def get_slot(self, name):
        return self._slots.get(name)


class TestAdaptivePriorityFocusSlot:
    def test_high_sustainability_keeps_priority_focus_in_required_slots(self):
        action = ValidateTripPlanningForm()
        tracker = FakeTracker(sustainability_level="high")
        result = asyncio.run(
            action.required_slots(list(DOMAIN_SLOTS), FakeDispatcher(), tracker, {})
        )
        assert "priority_focus" in result

    def test_low_sustainability_drops_priority_focus_from_required_slots(self):
        action = ValidateTripPlanningForm()
        tracker = FakeTracker(sustainability_level="low")
        result = asyncio.run(
            action.required_slots(list(DOMAIN_SLOTS), FakeDispatcher(), tracker, {})
        )
        assert "priority_focus" not in result

    def test_medium_sustainability_drops_priority_focus_from_required_slots(self):
        action = ValidateTripPlanningForm()
        tracker = FakeTracker(sustainability_level="medium")
        result = asyncio.run(
            action.required_slots(list(DOMAIN_SLOTS), FakeDispatcher(), tracker, {})
        )
        assert "priority_focus" not in result

    def test_unset_sustainability_drops_priority_focus_from_required_slots(self):
        # Defensive case: required_slots() can run before sustainability_level
        # is filled at all (e.g. Rasa computing the slot list on an earlier
        # turn) — must default to "not asking" rather than crashing or
        # wrongly keeping the extra question.
        action = ValidateTripPlanningForm()
        tracker = FakeTracker(sustainability_level=None)
        result = asyncio.run(
            action.required_slots(list(DOMAIN_SLOTS), FakeDispatcher(), tracker, {})
        )
        assert "priority_focus" not in result

    def test_every_other_slot_is_always_kept_regardless_of_branch(self):
        action = ValidateTripPlanningForm()
        for level in ("high", "low", "medium", None):
            tracker = FakeTracker(sustainability_level=level)
            result = asyncio.run(
                action.required_slots(list(DOMAIN_SLOTS), FakeDispatcher(), tracker, {})
            )
            for slot in DOMAIN_SLOTS:
                if slot == "priority_focus":
                    continue
                assert slot in result, f"{slot} wrongly dropped for sustainability_level={level!r}"

    def test_does_not_mutate_the_input_list(self):
        # required_slots() docstring/comment in actions.py promises it
        # copies domain_slots rather than mutating it in place — a caller
        # (Rasa's own FormAction) reuses that same list across calls, so a
        # mutation here would silently corrupt it for every other request.
        action = ValidateTripPlanningForm()
        original = list(DOMAIN_SLOTS)
        tracker = FakeTracker(sustainability_level="low")
        asyncio.run(action.required_slots(list(original), FakeDispatcher(), tracker, {}))
        assert original == DOMAIN_SLOTS
