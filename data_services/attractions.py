"""
data_services/attractions.py
=====================================================================
Notable places to visit near a destination. Live path queries
Overpass for museums/historic sites, then Wikipedia for a short
description of each (same approach as 04/05_*.py). If that's
unreachable, falls back to data/attractions.json — a small set of
REAL, well-known landmarks per major city (general knowledge facts,
not invented), rather than a fabricated specific place. Cities with
no curated entry get an honest generic note instead of a made-up
landmark name.
"""

from __future__ import annotations

import urllib.parse
from typing import List, Optional

import requests
from fastapi import APIRouter, Query
from pydantic import BaseModel

from . import common

router = APIRouter()

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
WIKIPEDIA_SUMMARY_URL = "https://en.wikipedia.org/api/rest_v1/page/summary/{title}"
VISITABLE_HISTORIC = "castle|monument|ruins|fort|archaeological_site|city_gate"
_CURATED = common.load_json("attractions.json")


class Attraction(BaseModel):
    name: str
    kind: Optional[str] = None
    summary: Optional[str] = None
    source_url: Optional[str] = None


class AttractionsResult(BaseModel):
    attractions: List[Attraction]
    source: str  # "live" | "curated" | "generic"


def _describe(title: str) -> Optional[dict]:
    safe_title = urllib.parse.quote(title.replace(" ", "_"))
    response = requests.get(WIKIPEDIA_SUMMARY_URL.format(title=safe_title), headers=common.HEADERS, timeout=6)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    data = response.json()
    text = data.get("extract", "")
    short = ". ".join(text.split(". ")[:2])
    if short and not short.endswith("."):
        short += "."
    return {"summary": short, "url": data.get("content_urls", {}).get("desktop", {}).get("page")}


def _live_attractions(lat: float, lon: float, radius_m: int, limit: int) -> Optional[List[dict]]:
    query = f"""
    [out:json][timeout:15];
    (
      nwr["tourism"="museum"](around:{radius_m},{lat},{lon});
      nwr["historic"~"^({VISITABLE_HISTORIC})$"](around:{radius_m},{lat},{lon});
    );
    out center {limit};
    """
    response = requests.post(OVERPASS_URL, data={"data": query}, headers=common.HEADERS, timeout=15)
    response.raise_for_status()
    elements = response.json().get("elements", [])

    sites = []
    for element in elements:
        tags = element.get("tags", {})
        if not tags.get("name"):
            continue
        sites.append({"name": tags["name"], "kind": tags.get("tourism") or tags.get("historic"), "wikipedia_tag": tags.get("wikipedia")})
    if not sites:
        return None

    enriched = []
    for site in sites[:3]:
        info = common.try_live(_describe, site.get("wikipedia_tag") or site["name"], attempts=1)
        enriched.append(
            {
                "name": site["name"],
                "kind": site.get("kind"),
                "summary": info["summary"] if info else None,
                "source_url": info["url"] if info else None,
            }
        )
    return enriched


@router.get("/attractions", response_model=AttractionsResult)
def find_attractions(
    lat: float = Query(...),
    lon: float = Query(...),
    city_name: str = Query(""),
    radius_m: int = Query(1500),
    limit: int = Query(8),
):
    live = common.try_live(_live_attractions, lat, lon, radius_m, limit)
    if live:
        return AttractionsResult(attractions=live, source="live")

    curated = _CURATED.get(city_name.strip().lower())
    if curated:
        return AttractionsResult(
            attractions=[Attraction(name=a["name"], kind=a["kind"], summary=a["summary"], source_url=None) for a in curated],
            source="curated",
        )

    return AttractionsResult(
        attractions=[
            Attraction(
                name=f"{city_name.strip().title() or 'This destination'}'s historic centre",
                kind="generic",
                summary="No curated or live attraction data available for this specific destination — worth checking a local guide.",
            )
        ],
        source="generic",
    )
