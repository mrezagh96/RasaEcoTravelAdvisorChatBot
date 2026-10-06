"""
data_services/transport.py
=====================================================================
Public-transport availability near a destination. Live Overpass data
(actual station/stop locations) is tried first. If it's unreachable,
falls back to data/transport_ratings.json — general, well-known
assessments of a city's transit quality (e.g. "Tokyo's metro is
extensive") rather than fabricated stop counts. Cities with no
curated rating get an honest "unknown, try again later" response
instead of a guess.
"""

from __future__ import annotations

from typing import List, Optional

import requests
from fastapi import APIRouter, Query
from pydantic import BaseModel

from . import common

router = APIRouter()

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
_RATINGS = common.load_json("transport_ratings.json")


class TransportResult(BaseModel):
    stop_count: Optional[int]
    summary: str
    source: str  # "live" | "curated" | "unknown"


def _live_transport(lat: float, lon: float, radius_m: int) -> Optional[List[dict]]:
    query = f"""
    [out:json][timeout:15];
    (
      node["railway"="station"](around:{radius_m},{lat},{lon});
      node["railway"="subway_entrance"](around:{radius_m},{lat},{lon});
      node["railway"="tram_stop"](around:{radius_m},{lat},{lon});
    );
    out body 25;
    """
    response = requests.post(OVERPASS_URL, data={"data": query}, headers=common.HEADERS, timeout=15)
    response.raise_for_status()
    return response.json().get("elements", [])


def _summarise(count: int) -> str:
    if count >= 10:
        return "excellent — you likely won't need a car"
    if count >= 3:
        return "reasonable, but check routes for your specific trip"
    return "limited — factor extra transport emissions into your planning"


@router.get("/transport", response_model=TransportResult)
def find_transport(
    lat: float = Query(...),
    lon: float = Query(...),
    city_name: str = Query(""),
    radius_m: int = Query(1500),
):
    live = common.try_live(_live_transport, lat, lon, radius_m)
    if live is not None:
        count = len(live)
        return TransportResult(stop_count=count, summary=_summarise(count), source="live")

    curated = _RATINGS.get(city_name.strip().lower())
    if curated:
        return TransportResult(
            stop_count=curated["stop_count_estimate"],
            summary=f"{curated['rating']} — {curated['note']}",
            source="curated",
        )

    return TransportResult(stop_count=None, summary="unknown — live transit data wasn't reachable and no curated rating exists for this city", source="unknown")
