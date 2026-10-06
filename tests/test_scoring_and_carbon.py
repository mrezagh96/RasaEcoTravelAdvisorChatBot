"""
tests/test_scoring_and_carbon.py
=====================================================================
A starting point for Task 5 (Testing), not the finished suite — these
cover the parts of the project with no network dependency, so they run
instantly and deterministically: the weighted scoring function
(actions/scoring.py) and the local carbon factor table
(actions/api_clients.py). Mocked-API tests for actions.py's
form-validation and orchestrator actions are the next thing to add here
together, per the project plan.

Run with:  pytest tests/ -v
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from actions import api_clients, scoring


class TestScoring:
    def test_low_sustainability_prefers_cheapest(self):
        options = [
            {"name": "Green", "carbon_kg": 5, "price": 300},
            {"name": "Cheap", "carbon_kg": 25, "price": 100},
        ]
        ranked = scoring.rank_options(options, "low")
        assert ranked[0]["name"] == "Cheap"
        assert ranked[0]["tier"] == "green"

    def test_high_sustainability_prefers_lowest_carbon(self):
        options = [
            {"name": "Green", "carbon_kg": 5, "price": 300},
            {"name": "Cheap", "carbon_kg": 25, "price": 100},
        ]
        ranked = scoring.rank_options(options, "high")
        assert ranked[0]["name"] == "Green"

    def test_priority_focus_carbon_sharpens_the_high_base_weight(self):
        carbon_w_base, _ = scoring.get_weights("high")
        carbon_w_focus, _ = scoring.get_weights("high", "carbon")
        assert carbon_w_focus > carbon_w_base

    def test_empty_options_returns_empty(self):
        assert scoring.rank_options([], "medium") == []

    def test_single_option_is_always_green(self):
        ranked = scoring.rank_options([{"name": "Only", "carbon_kg": 10, "price": 50}], "medium")
        assert ranked[0]["tier"] == "green"

    def test_identical_values_do_not_divide_by_zero(self):
        options = [
            {"name": "A", "carbon_kg": 10, "price": 100},
            {"name": "B", "carbon_kg": 10, "price": 100},
        ]
        ranked = scoring.rank_options(options, "medium")
        assert all(o["score"] == ranked[0]["score"] for o in ranked)


class TestCarbonTable:
    def test_walk_and_cycle_are_zero_emission(self):
        assert api_clients.estimate_carbon_kg("walk", 100) == 0
        assert api_clients.estimate_carbon_kg("cycle", 100) == 0

    def test_flight_short_is_the_worst_of_the_default_comparison_set(self):
        ranked = api_clients.compare_modes(1000)
        assert ranked[-1][0] == "flight_short"

    def test_unknown_mode_raises_api_error(self):
        try:
            api_clients.estimate_carbon_kg("teleporter", 100)
            assert False, "expected ApiError"
        except api_clients.ApiError:
            pass

    def test_carbon_scales_linearly_with_distance(self):
        short = api_clients.estimate_carbon_kg("train", 100)
        long = api_clients.estimate_carbon_kg("train", 200)
        assert long == short * 2


class TestHaversine:
    def test_same_point_is_zero_distance(self):
        d = api_clients.haversine_km(52.52, 13.405, 52.52, 13.405)
        assert d == 0

    def test_berlin_to_lisbon_is_roughly_correct(self):
        # Known real-world great-circle distance is ~2300 km.
        d = api_clients.haversine_km(52.52, 13.405, 38.7223, -9.1393)
        assert 2200 < d < 2400
