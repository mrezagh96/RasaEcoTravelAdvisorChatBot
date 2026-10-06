"""
data_services/common.py
=====================================================================
Shared infrastructure for every router in this package: the HTTP
headers used for outbound calls, a retry decorator, a tiny in-memory
TTL cache, and the haversine distance formula. Every router follows the
same shape: try the real, live API first (short timeout, one retry);
if that fails, fall back to this package's own curated data
(data_services/data/*.json) so the bot always gets a useful, honestly
labelled answer instead of a dead end.
"""

from __future__ import annotations

import functools
import json
import time
from math import asin, cos, radians, sin, sqrt
from pathlib import Path
from typing import Any, Callable, Dict, Tuple

import requests

HEADERS = {
    "User-Agent": "BSBI-EcoTravelAdvisor/1.0 (MSc coursework; contact via GitHub repo)"
}

DATA_DIR = Path(__file__).parent / "data"


def load_json(filename: str) -> Any:
    with open(DATA_DIR / filename, "r", encoding="utf-8") as fh:
        return json.load(fh)


_CACHE: Dict[str, Tuple[float, Any]] = {}
_CACHE_TTL_SECONDS = 60 * 60 * 6


def cache_get(key: str):
    hit = _CACHE.get(key)
    if not hit:
        return None
    stored_at, value = hit
    if time.time() - stored_at > _CACHE_TTL_SECONDS:
        _CACHE.pop(key, None)
        return None
    return value


def cache_set(key: str, value: Any) -> None:
    _CACHE[key] = (time.time(), value)


def try_live(func: Callable, *args, attempts: int = 2, timeout_each: float = 8.0, **kwargs):
    """Calls a live-API function up to `attempts` times with a short
    per-attempt budget, returning None (never raising) on total failure
    so every router can fall straight into its curated-data path without
    a try/except at every call site. `timeout_each` is documentation
    here — the actual per-request timeout is set inside each `func`
    (requests' timeout=), this just bounds how many attempts we spend
    before giving up and falling back."""
    last_exc = None
    for attempt in range(attempts):
        try:
            return func(*args, **kwargs)
        except (requests.exceptions.RequestException, KeyError, ValueError) as exc:
            last_exc = exc
            if attempt < attempts - 1:
                time.sleep(0.5)
    return None


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    phi1, phi2 = radians(lat1), radians(lat2)
    d_phi = radians(lat2 - lat1)
    d_lambda = radians(lon2 - lon1)
    a = sin(d_phi / 2) ** 2 + cos(phi1) * cos(phi2) * sin(d_lambda / 2) ** 2
    return 2 * r * asin(sqrt(a))
