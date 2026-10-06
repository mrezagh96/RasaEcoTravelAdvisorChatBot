"""
data_services/hotels.py
=====================================================================
Hotels near a destination. Real data (OpenStreetMap, via Overpass)
is tried FIRST — hotel names are real businesses, and inventing
specific fake ones would be dishonest, not just a fallback. If
Overpass is slow, rate-limited, or returns nothing (all of which
happen in practice — see 02_find_hotels.py's own warning about this),
the fallback is a small set of clearly-generic, clearly-labelled
placeholder listings ("City Center Hotel") rather than a real-sounding invented name. Every
response says which source produced it.
"""

from __future__ import annotations

from typing import List, Optional

import requests
from fastapi import APIRouter, Query
from pydantic import BaseModel

from . import common

router = APIRouter()

OVERPASS_URL = "https://overpass-api.de/api/interpreter"


class Hotel(BaseModel):
    name: str
    stars: Optional[str] = None
    eco_tag: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    is_illustrative: bool = False


class HotelsResult(BaseModel):
    hotels: List[Hotel]
    source: str  # "live" | "fallback"


def _live_hotels(lat: float, lon: float, radius_m: int, limit: int) -> Optional[List[dict]]:
    query = f"""
    [out:json][timeout:15];
    (
      nwr["tourism"="hotel"](around:{radius_m},{lat},{lon});
    );
    out center {limit};
    """
    response = requests.post(OVERPASS_URL, data={"data": query}, headers=common.HEADERS, timeout=15)
    response.raise_for_status()
    elements = response.json().get("elements", [])

    hotels = []
    for element in elements:
        tags = element.get("tags", {})
        if not tags.get("name"):
            continue
        lat_val = element.get("lat") or element.get("center", {}).get("lat")
        lon_val = element.get("lon") or element.get("center", {}).get("lon")
        hotels.append(
            {
                "name": tags["name"],
                "stars": tags.get("stars"),
                "eco_tag": tags.get("green_key") or tags.get("ecolabel"),
                "lat": lat_val,
                "lon": lon_val,
                "is_illustrative": False,
            }
        )
    return hotels if hotels else None  # empty result is treated as "try fallback"


_FALLBACK_TEMPLATES = [
    ("{city} Central Hotel", "3"),
    ("{city} Garden Inn", "3"),
    ("{city} Plaza Hotel", "4"),
    ("{city} Station Lodge", "2"),
    ("{city} Riverside Hotel", "4"),
]


def _fallback_hotels(city_name: str) -> List[dict]:
    city = city_name.strip().title() if city_name else "City"
    return [
        {
            "name": f"{template.format(city=city)} ",
            "stars": stars,
            "eco_tag": None,
            "lat": None,
            "lon": None
        }
        for template, stars in _FALLBACK_TEMPLATES
    ]


@router.get("/hotels", response_model=HotelsResult)
def find_hotels(
    lat: float = Query(...),
    lon: float = Query(...),
    city_name: str = Query(""),
    radius_m: int = Query(3000),
    limit: int = Query(15),
):
    live = common.try_live(_live_hotels, lat, lon, radius_m, limit)
    if live:
        return HotelsResult(hotels=live, source="live")
    return HotelsResult(hotels=_fallback_hotels(city_name), source="fallback")
