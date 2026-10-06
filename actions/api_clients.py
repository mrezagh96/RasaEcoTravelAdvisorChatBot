"""
api_clients.py — Eco-Travel Advisor
=====================================================================
Every function here talks to exactly ONE place: the unified
data_services app (http://localhost:8000 by default — see
DATA_SERVICES_URL below). All the "try the real API, fall back to
curated data" resilience logic that used to be scattered across this
file now lives in data_services/ instead, one router per domain (see
data_services/main.py's module docstring for why). This file is now a
thin, uniform REST client — the same shape for every call, easy to
mock in tests, easy to reason about.

The only two functions that stay purely local (no HTTP at all) are
`estimate_carbon_kg` and `haversine_km` — trivial arithmetic that
doesn't benefit from a network round-trip even to localhost, kept
here so actions.py always has a last-resort carbon/distance figure
even in the (very unlikely) event data_services itself is down.
"""

from __future__ import annotations

import os
from math import asin, cos, radians, sin, sqrt
from typing import Any, Dict, List, Optional, Tuple

import requests

DATA_SERVICES_URL = os.environ.get("DATA_SERVICES_URL", "http://localhost:8000")


class ApiError(Exception):
    """Raised whenever data_services could not answer — always carries a
    short, user-safe explanation actions.py can hand straight to the
    dispatcher instead of leaking a stack trace into the conversation."""


def _get(path: str, params: Dict[str, Any], timeout: float = 20.0) -> Dict[str, Any]:
    try:
        response = requests.get(f"{DATA_SERVICES_URL}{path}", params=params, timeout=timeout)
    except requests.exceptions.RequestException as exc:
        raise ApiError(f"data_services unreachable for {path}: {exc}") from exc
    if response.status_code == 404:
        return None  # "not found" is a normal, expected outcome for geocode
    try:
        response.raise_for_status()
    except requests.exceptions.HTTPError as exc:
        raise ApiError(f"data_services returned {response.status_code} for {path}: {exc}") from exc
    return response.json()


# -----------------------------------------------------------------------
# Geocoding
# -----------------------------------------------------------------------

def geocode(place: str) -> Optional[Dict[str, Any]]:
    """Turn a place name into {lat, lon, display_name, source}. None if
    not found. ~170 major cities resolve instantly from data_services'
    curated table (source="curated"); anything else tries a live
    Nominatim call behind the scenes."""
    return _get("/geocode", {"place": place})


_cities_list_cache: Optional[Dict[str, List[str]]] = None


def list_cities() -> Dict[str, List[str]]:
    """The full curated city list grouped by continent. Cached in-memory
    after the first call (it's static curated data, not live) so random
    city-button sampling on every trip-planning question doesn't hit
    data_services repeatedly for the same 170-city list."""
    global _cities_list_cache
    if _cities_list_cache is None:
        result = _get("/geocode/cities", {})
        _cities_list_cache = result["cities"] if result else {}
    return _cities_list_cache


def match_city(candidate: str) -> Optional[Dict[str, Any]]:
    """Typo-tolerant lookup for a city-name candidate (see
    actions/city_extractor.py for where `candidate` usually comes from).
    Returns {found, is_exact, matched_name, lat, lon, display_name,
    source}. `is_exact=False` means it's a fuzzy match that should be
    confirmed with the user ("Did you mean X?") before being accepted —
    see ValidateTripPlanningForm.validate_origin_city / _destination_city."""
    return _get("/geocode/suggest", {"query": candidate})


# -----------------------------------------------------------------------
# Hotels
# -----------------------------------------------------------------------

def find_hotels(lat: float, lon: float, city_name: str = "", radius_m: int = 3000, limit: int = 15) -> List[Dict[str, Any]]:
    result = _get("/hotels", {"lat": lat, "lon": lon, "city_name": city_name, "radius_m": radius_m, "limit": limit})
    return result["hotels"] if result else []


# -----------------------------------------------------------------------
# Transport
# -----------------------------------------------------------------------

def find_transport(lat: float, lon: float, city_name: str = "", radius_m: int = 1500) -> Dict[str, Any]:
    """Returns {stop_count, summary, source} directly — data_services now
    does the "count stops -> pick a summary sentence" step itself, so
    callers don't need a separate summarise() pass any more."""
    result = _get("/transport", {"lat": lat, "lon": lon, "city_name": city_name, "radius_m": radius_m})
    return result or {"stop_count": None, "summary": "unavailable right now", "source": "error"}


# -----------------------------------------------------------------------
# Attractions
# -----------------------------------------------------------------------

def find_attractions(lat: float, lon: float, city_name: str = "", radius_m: int = 1500, limit: int = 8) -> List[Dict[str, Any]]:
    """Returns a list of {name, kind, summary, source_url} — already
    enriched with a short description by data_services, no separate
    Wikipedia lookup step needed here any more."""
    result = _get("/attractions", {"lat": lat, "lon": lon, "city_name": city_name, "radius_m": radius_m, "limit": limit})
    return result["attractions"] if result else []


# -----------------------------------------------------------------------
# Weather
# -----------------------------------------------------------------------

def get_weather(lat: float, lon: float) -> Optional[Dict[str, Any]]:
    """Returns {temperature_c, precipitation_mm, wind_kmh, advice,
    source} directly — the travel-advice sentence is now generated
    inside data_services, right next to the seasonal-estimate fallback
    that also needs to produce one."""
    return _get("/weather", {"lat": lat, "lon": lon})


def get_weather_forecast(lat: float, lon: float, start_date: str, num_days: int = 1) -> Optional[Dict[str, Any]]:
    """Per-day forecast for a trip. `start_date` is YYYY-MM-DD. Returns
    {"days": [{"date", "temperature_max_c", "precipitation_mm",
    "advice", "source"}, ...]} — live where Open-Meteo's ~16-day window
    covers it, a seasonal estimate for that day's month otherwise
    (correctly honest for a trip booked further out than any live
    forecast can reach)."""
    return _get("/weather/forecast", {"lat": lat, "lon": lon, "start_date": start_date, "num_days": num_days})


# -----------------------------------------------------------------------
# Currency
# -----------------------------------------------------------------------

def convert(amount: float, from_currency: str, to_currency: str) -> Tuple[float, float, str]:
    result = _get("/currency/convert", {"amount": amount, "from_currency": from_currency, "to_currency": to_currency})
    if not result:
        raise ApiError(f"No rate available for {from_currency} -> {to_currency}")
    return result["converted"], result["rate"], result["date"]


# -----------------------------------------------------------------------
# Carbon
# -----------------------------------------------------------------------

# Kept as a local constant (not an HTTP call) — see module docstring.
# Indicative gCO2e per passenger-km, cited from UK DESNZ/DEFRA GHG
# conversion factors and the EEA transport emissions dataset (see
# README "Data sources"). Mirrors data_services/carbon.py exactly;
# that endpoint stays available too, for anything that wants it over
# HTTP (e.g. a future frontend), but actions.py's own hot path doesn't
# need a network round-trip for a multiplication.
EMISSION_FACTORS = {
    "walk": 0, "cycle": 0, "train": 35, "coach": 27,
    "electric_car": 50, "car_petrol": 170, "ferry": 115,
    "flight_short": 250, "flight_long": 150,
}


def estimate_carbon_kg(mode: str, distance_km: float) -> float:
    if mode not in EMISSION_FACTORS:
        raise ApiError(f"Unknown transport mode: {mode}")
    return EMISSION_FACTORS[mode] * distance_km / 1000.0


def compare_modes(distance_km: float, modes: Optional[List[str]] = None) -> List[Tuple[str, float]]:
    modes = modes or ["train", "coach", "car_petrol", "flight_short"]
    results = [(m, estimate_carbon_kg(m, distance_km)) for m in modes]
    return sorted(results, key=lambda pair: pair[1])


# -----------------------------------------------------------------------
# Surface transport fare estimate (train / coach)
# -----------------------------------------------------------------------
# No free, no-signup fare API exists for European rail/coach (see README
# "Data sources" — same reasoning as flights.py's own missing live
# source). Leaving the price as None left the user with nothing to
# compare against when deciding between travel modes, so — same
# disclosed-formula pattern data_services/flights.py already uses for
# ANY uncurated flight route (a distance-based base-fee + per-km rate,
# constants declared and justified right here, not presented as a real
# quote) — this gives train/coach an honest, usable ESTIMATE instead of
# "fare not available".
#
# Rates are rough, defensible order-of-magnitude figures for European
# intercity travel (per-km cost drops as distance grows, same shape as
# flights.py's short/long-haul split): a long-distance coach (FlixBus-
# style) is generally the cheapest motorised option, well below rail;
# a standard-class rail ticket runs several times a coach fare per km,
# reflecting typical operator pricing (Deutsche Bahn, Trenitalia, SNCF,
# ...) even though any single real fare varies a lot with how far in
# advance it's booked. Prices are rounded to the nearest 5 EUR, same as
# flights.py, so the figure reads as the estimate it is rather than a
# falsely precise quote.
_SURFACE_FARE_RATES = {
    "coach": {"base_fee_eur": 8.0, "rate_per_km": 0.045},
    "train": {"base_fee_eur": 12.0, "rate_per_km": 0.11},
}


def estimate_surface_fare_eur(mode: str, distance_km: float) -> float:
    """Distance-based fare ESTIMATE for "train" or "coach", in EUR —
    see the constants above for the reasoning. Raises ApiError for any
    other mode (flights have their own real endpoint; walk/cycle/car
    have no fare concept here)."""
    rates = _SURFACE_FARE_RATES.get(mode)
    if rates is None:
        raise ApiError(f"No fare estimate formula for mode: {mode}")
    price = rates["base_fee_eur"] + rates["rate_per_km"] * distance_km
    return round(price / 5) * 5


# -----------------------------------------------------------------------
# Flights
# -----------------------------------------------------------------------

def get_flight_estimate(
    origin_name: str, origin_lat: float, origin_lon: float,
    destination_name: str, destination_lat: float, destination_lon: float,
) -> Dict[str, Any]:
    result = _get(
        "/flights/estimate",
        {
            "origin_name": origin_name, "origin_lat": origin_lat, "origin_lon": origin_lon,
            "destination_name": destination_name, "destination_lat": destination_lat, "destination_lon": destination_lon,
        },
    )
    if not result:
        raise ApiError(f"No flight estimate available for {origin_name} -> {destination_name}")
    return result


# -----------------------------------------------------------------------
# Pure local helper (no HTTP) — kept for the same reason as the carbon
# table above: trivial math, used as a last-resort fallback if
# data_services itself is unreachable when computing the headline
# distance for carbon comparisons.
# -----------------------------------------------------------------------

def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    phi1, phi2 = radians(lat1), radians(lat2)
    d_phi = radians(lat2 - lat1)
    d_lambda = radians(lon2 - lon1)
    a = sin(d_phi / 2) ** 2 + cos(phi1) * cos(phi2) * sin(d_lambda / 2) ** 2
    return 2 * r * asin(sqrt(a))