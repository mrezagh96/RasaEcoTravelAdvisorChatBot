"""
data_services/geocode.py
=====================================================================
Geocoding, curated-first. ~170 major world cities are looked up
INSTANTLY from data/cities.json — no network call at all, so common
destinations (including ones DIET's NLU might not recognise as a
clean entity, like Tabriz) never depend on Nominatim being fast,
un-rate-limited, or even reachable. Anything not curated falls back
to a live Nominatim call, exactly like the original
01_geocode_place.py sample script.

`/geocode/suggest` additionally fuzzy-matches typos against the
curated list (e.g. "frankfürt", "newyork", "teheran", "brrlin" all
resolve to the correct city) using stdlib difflib — no extra
dependency, no ML model, just edit-distance-style similarity on
accent-normalised strings. It never silently "corrects" a typo: it
reports whether the match was exact or fuzzy so the caller (see
actions/actions.py) can ask the user to confirm a fuzzy one rather
than guessing on their behalf.
"""

from __future__ import annotations

import difflib
import unicodedata
from typing import Optional

import requests
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from . import common

router = APIRouter()

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
_CITIES = common.load_json("cities.json")

# similarity threshold for a fuzzy match to be offered at all — see the
# module docstring; 0.6 was checked against frankfürt/newyork/teheran/
# brrlin-style typos and correctly resolves all of them without also
# matching genuinely different short city names to each other.
FUZZY_CUTOFF = 0.6


def _normalise(s: str) -> str:
    """Strips accents (frankfürt -> frankfurt) and lowercases, so accent
    differences don't count against the similarity score."""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    return s.strip().lower()


_NORMALISED_KEYS = {_normalise(k): k for k in _CITIES.keys()}

# ---- known exonym/native-name aliases for a few already-curated cities
# whose JSON key uses an English name that differs from the name a
# German (or German-fluent) user is likely to actually type — added
# directly to the normalised-key lookup (NOT to _CITIES itself, so
# /geocode/cities' displayed list is completely unaffected, no duplicate
# "city" ever shows up there).
#
# ROOT CAUSE this fixes (found via real testing, not theory, while
# verifying this round's German-city expansion): geocode_suggest's fuzzy
# step (difflib.get_close_matches) only ever sees whichever curated key
# happens to be textually CLOSEST overall — it has no notion of "this is
# the same city's own native name", just string similarity. Before this
# round's ~183 new German cities were added, "köln" was simply the
# closest match difflib could find (nothing else was close enough to
# beat it, so it still resolved to Cologne, just as a fuzzy rather than
# an exact hit). Confirmed via a real call to /geocode/suggest that
# adding "Koblenz" (a genuine, separate, real German city — see
# cities.json) as a new curated entry changed that: "köln"/"koln" is
# textually closer to "koblenz" than to "cologne", so it silently
# started resolving to the WRONG city. The exact same thing happened to
# "wien" once "Witten" was added (closer to "witten" than to "vienna").
# "münchen"/"nürnberg" still happen to resolve correctly today (nothing
# added this round out-competes them) but are exactly as fragile — a
# future addition could just as easily break them the same way — so
# they're fixed here too rather than left as an accident waiting to
# recur. This is the general fix (an exact alias, independent of
# difflib's threshold entirely) rather than a patch for only the two
# cases that happened to break today.
_KNOWN_CITY_ALIASES = {
    "koln": "cologne",           # Köln
    "wien": "vienna",
    "munchen": "munich",         # München
    "nurnberg": "nuremberg",     # Nürnberg
}
for _alias, _canonical_key in _KNOWN_CITY_ALIASES.items():
    if _canonical_key in _CITIES:
        _NORMALISED_KEYS.setdefault(_alias, _canonical_key)


class GeocodeResult(BaseModel):
    lat: float
    lon: float
    display_name: str
    source: str  # "curated" | "live" | "live_cached"


class SuggestResult(BaseModel):
    found: bool
    is_exact: bool = False
    matched_key: Optional[str] = None       # e.g. "berlin" — the curated dict key
    matched_name: Optional[str] = None      # e.g. "Berlin" — display form for a "Did you mean?" prompt
    lat: Optional[float] = None
    lon: Optional[float] = None
    display_name: Optional[str] = None
    source: str = "not_found"               # "curated_exact" | "curated_fuzzy" | "live" | "not_found"


def _live_geocode(place: str) -> Optional[dict]:
    """Live Nominatim lookup, biased toward actual places people live in
    (cities, towns, villages) rather than any matching feature at all.

    `featureType=settlement` restricts results to the address layer's
    "state down to neighbourhood" band — without it, a query that
    doesn't closely match a curated or well-known city name can return
    Nominatim's best guess at ANYTHING (a park, a building, a shop),
    and previously this endpoint used that result's `display_name`
    directly — a full, comma-separated address string, not a place
    name (e.g. "Jardin botanique de Genève, 1, Pâquis, Chambésy, ...",
    for what should have simply become "Geneva"). Confirmed as a real,
    reproduced bug during live testing, not theory.

    `addressdetails=1` is what makes the fix possible: it returns a
    structured `address` object with a clean city/town/village field,
    used below instead of the verbose display_name whenever it's
    present.
    """
    response = requests.get(
        NOMINATIM_URL,
        params={
            "q": place, "format": "json", "limit": 1,
            "featureType": "settlement", "addressdetails": 1,
        },
        headers=common.HEADERS,
        timeout=8,
    )
    response.raise_for_status()
    results = response.json()
    if not results:
        return None
    best = results[0]
    address = best.get("address", {}) or {}
    clean_name = (
        address.get("city") or address.get("town") or address.get("village")
        or address.get("municipality") or address.get("county")
        # Genuinely no structured settlement field at all (shouldn't
        # happen with featureType=settlement, but never trust a live
        # API to be 100% consistent) — the first comma-separated
        # component of display_name is still far cleaner than the
        # whole string.
        or best["display_name"].split(",")[0].strip()
    )
    return {"lat": float(best["lat"]), "lon": float(best["lon"]), "display_name": clean_name}


class CitiesListResult(BaseModel):
    cities: dict  # {continent: [city display names]}


# Rough continent grouping by country, for the "cities list" sub-button
# (per-continent categorisation was an explicit requirement) — a simple
# lookup table over the curated countries rather than a geocoding call,
# since this is just for organising an already-curated list for display.
_CONTINENT_BY_COUNTRY = {
    "Germany": "Europe", "United Kingdom": "Europe", "Ireland": "Europe", "France": "Europe",
    "Spain": "Europe", "Italy": "Europe", "Portugal": "Europe", "Netherlands": "Europe",
    "Belgium": "Europe", "Austria": "Europe", "Switzerland": "Europe", "Denmark": "Europe",
    "Sweden": "Europe", "Norway": "Europe", "Finland": "Europe", "Poland": "Europe",
    "Czech Republic": "Europe", "Hungary": "Europe", "Greece": "Europe", "Romania": "Europe",
    "Bulgaria": "Europe", "Croatia": "Europe", "Serbia": "Europe", "Ukraine": "Europe",
    "Russia": "Europe",
    "Turkey": "Middle East", "Iran": "Middle East", "United Arab Emirates": "Middle East",
    "Qatar": "Middle East", "Saudi Arabia": "Middle East", "Kuwait": "Middle East",
    "Iraq": "Middle East", "Jordan": "Middle East", "Lebanon": "Middle East",
    "Israel": "Middle East", "Oman": "Middle East",
    "Japan": "Asia", "South Korea": "Asia", "China": "Asia", "Taiwan": "Asia",
    "Singapore": "Asia", "Thailand": "Asia", "Malaysia": "Asia", "Indonesia": "Asia",
    "Philippines": "Asia", "Vietnam": "Asia", "India": "Asia", "Pakistan": "Asia",
    "Bangladesh": "Asia", "Sri Lanka": "Asia", "Nepal": "Asia", "Kazakhstan": "Asia",
    "Uzbekistan": "Asia",
    "Egypt": "Africa", "Morocco": "Africa", "Tunisia": "Africa", "Algeria": "Africa",
    "Nigeria": "Africa", "Kenya": "Africa", "Ethiopia": "Africa", "Ghana": "Africa",
    "Senegal": "Africa", "South Africa": "Africa", "Uganda": "Africa", "Tanzania": "Africa",
    "United States": "North America", "Canada": "North America", "Mexico": "North America",
    "Cuba": "North America", "Panama": "North America",
    "Colombia": "South America", "Peru": "South America", "Chile": "South America",
    "Argentina": "South America", "Brazil": "South America", "Venezuela": "South America",
    "Ecuador": "South America",
    "Australia": "Oceania", "New Zealand": "Oceania",
}


@router.get("/geocode/cities", response_model=CitiesListResult)
def list_cities():
    """The full curated city list, grouped by continent — backs both the
    Trip Assistant's "cities list" sub-button and the random destination/
    origin button sampling in actions.py."""
    grouped: dict = {}
    for key, info in _CITIES.items():
        continent = _CONTINENT_BY_COUNTRY.get(info["country"], "Other")
        grouped.setdefault(continent, []).append(key.title())
    for names in grouped.values():
        names.sort()
    return CitiesListResult(cities=grouped)


@router.get("/geocode", response_model=GeocodeResult)
def geocode(place: str = Query(...)):
    key = place.strip().lower()

    curated = _CITIES.get(key)
    if curated:
        return GeocodeResult(lat=curated["lat"], lon=curated["lon"], display_name=curated["display_name"], source="curated")

    cache_key = f"geocode:{key}"
    cached = common.cache_get(cache_key)
    if cached:
        return GeocodeResult(**cached, source="live_cached")

    live = common.try_live(_live_geocode, place)
    if live:
        common.cache_set(cache_key, live)
        return GeocodeResult(**live, source="live")

    # Genuinely not found (or Nominatim unreachable) — a 404, not a fake
    # (0, 0) result, so callers can tell "no such place" apart from a
    # real coordinate near Null Island.
    raise HTTPException(status_code=404, detail=f"No place found for '{place}'")


# BUGFIX (real user report + real execution, actions/city_extractor.py's
# lenient trigger extraction): a garbage 1-character candidate ("a",
# captured from "...to go to a travel...") reached this endpoint with
# nothing stopping it. The curated-exact and curated-fuzzy steps below
# correctly found nothing for it (a single letter can't clear
# FUZZY_CUTOFF against any real city name), but the LIVE Nominatim
# fallback is trusted AS-IS with no confirmation step at all (see this
# function's own docstring) — and Nominatim's free-text search has no
# minimum-length floor of its own either, so an under-constrained
# one-letter query can non-deterministically resolve to whatever place
# its ranking picks that moment (confirmed via a real, live call: it
# returned nothing in one run, and could just as easily return a real
# but totally unrelated settlement in another — this is exactly the
# reported failure, once observed resolving to "Salta"). No real city,
# anywhere, has a name shorter than 2 characters, so a query that short
# is rejected outright, before even trying the live lookup — closing
# this off at the source for every caller of this endpoint, not just
# the one extractor path that happened to trigger it.
_MIN_QUERY_LENGTH = 2


@router.get("/geocode/suggest", response_model=SuggestResult)
def geocode_suggest(query: str = Query(...)):
    """Typo-tolerant lookup. Order: exact curated match -> fuzzy curated
    match (needs confirmation) -> live Nominatim (trusted as-is, no
    curated list to fuzzy-match against) -> not found."""
    raw = query.strip()
    norm = _normalise(raw)

    if len(norm.replace(" ", "")) < _MIN_QUERY_LENGTH:
        return SuggestResult(found=False, source="not_found")

    exact_key = _NORMALISED_KEYS.get(norm)
    if exact_key:
        c = _CITIES[exact_key]
        return SuggestResult(
            found=True, is_exact=True, matched_key=exact_key, matched_name=exact_key.title(),
            lat=c["lat"], lon=c["lon"], display_name=c["display_name"], source="curated_exact",
        )

    close = difflib.get_close_matches(norm, _NORMALISED_KEYS.keys(), n=1, cutoff=FUZZY_CUTOFF)
    if close:
        matched_key = _NORMALISED_KEYS[close[0]]
        c = _CITIES[matched_key]
        return SuggestResult(
            found=True, is_exact=False, matched_key=matched_key, matched_name=matched_key.title(),
            lat=c["lat"], lon=c["lon"], display_name=c["display_name"], source="curated_fuzzy",
        )

    live = common.try_live(_live_geocode, raw)
    if live:
        return SuggestResult(found=True, is_exact=True, matched_name=live["display_name"], source="live", **live)

    return SuggestResult(found=False, source="not_found")