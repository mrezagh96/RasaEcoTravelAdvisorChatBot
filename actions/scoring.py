"""
scoring.py — Eco-Travel Advisor
=====================================================================
The one piece of this project with no API behind it at all: turning a
carbon figure, a price, and a user preference into a single ranking.
The brief is explicit that this weighting is a design decision to be
made (and defended in the report), not something an API hands you —
see 08_carbon_estimate.py's closing note. This module is that decision,
written down and kept separate from actions.py so it can be unit
tested in isolation and cited on its own in the report.

Method: min-max normalise each attribute across the candidate set (so
the winner is always relative to what's actually on offer for this
trip, not an arbitrary global threshold), invert so "lower is better"
becomes "higher score is better", then take a weighted sum. Weights
come from the user's stated sustainability_level, sharpened further by
the adaptive priority_focus question when it was asked.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

# sustainability_level -> (carbon_weight, price_weight)
BASE_WEIGHTS: Dict[str, Tuple[float, float]] = {
    "high": (0.70, 0.30),
    "medium": (0.50, 0.50),
    "low": (0.25, 0.75),
}

# priority_focus (only ever asked when sustainability_level == "high")
# sharpens or softens that base 0.70/0.30 split.
PRIORITY_FOCUS_WEIGHTS: Dict[str, Tuple[float, float]] = {
    "carbon": (0.85, 0.15),
    "cost": (0.55, 0.45),
    "balanced": (0.70, 0.30),
}


def get_weights(sustainability_level: str, priority_focus: str = "") -> Tuple[float, float]:
    """Returns (carbon_weight, price_weight), always summing to 1.0."""
    if priority_focus in PRIORITY_FOCUS_WEIGHTS:
        return PRIORITY_FOCUS_WEIGHTS[priority_focus]
    return BASE_WEIGHTS.get(sustainability_level, BASE_WEIGHTS["medium"])


def _normalise_inverted(values: List[float]) -> List[float]:
    """Min-max normalise, then flip so the SMALLEST raw value scores 1.0
    (best) and the LARGEST scores 0.0 (worst). A constant list scores
    every entry 1.0 rather than dividing by zero."""
    lo, hi = min(values), max(values)
    if hi == lo:
        return [1.0 for _ in values]
    return [1.0 - ((v - lo) / (hi - lo)) for v in values]


def rank_options(
    options: List[Dict[str, Any]],
    sustainability_level: str,
    priority_focus: str = "",
    carbon_key: str = "carbon_kg",
    price_key: str = "price",
) -> List[Dict[str, Any]]:
    """Annotates each option with `score` (0-1, higher = better) and
    `tier` ("green"/"amber"/"red", relative to the other candidates in
    THIS list), then returns the list sorted best-first.

    Every option dict is returned with two new keys added — nothing
    else about the input is touched, so callers can pass through
    whatever else they need to display (name, duration, etc.).
    """
    if not options:
        return []

    carbon_weight, price_weight = get_weights(sustainability_level, priority_focus)

    carbon_values = [float(o.get(carbon_key, 0) or 0) for o in options]
    price_values = [float(o.get(price_key, 0) or 0) for o in options]

    carbon_scores = _normalise_inverted(carbon_values)
    price_scores = _normalise_inverted(price_values)

    scored = []
    for option, c_score, p_score in zip(options, carbon_scores, price_scores):
        combined = round(carbon_weight * c_score + price_weight * p_score, 4)
        scored.append({**option, "score": combined})

    scored.sort(key=lambda o: o["score"], reverse=True)

    # Tier is based on relative RANK within this candidate set (thirds),
    # not an arbitrary absolute number — five near-identical hotels should
    # not all be painted red just because one has a slightly bigger carbon
    # figure than the others.
    n = len(scored)
    for idx, option in enumerate(scored):
        if n <= 1:
            option["tier"] = "green"
        else:
            pct = idx / (n - 1)
            option["tier"] = "green" if pct <= 1 / 3 else "amber" if pct <= 2 / 3 else "red"

    return scored


def tier_emoji(tier: str) -> str:
    return {"green": "🟢", "amber": "🟡", "red": "🔴"}.get(tier, "⚪")
