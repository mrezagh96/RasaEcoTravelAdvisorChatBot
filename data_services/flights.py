"""
data_services/flights.py
=====================================================================
Self-hosted flight estimate — see the original module docstring in
the (now retired) flight_data_service/main.py for the full reasoning
on why this exists instead of calling Amadeus or a third party.
Unchanged logic, just relocated so it's one router among many in the
single combined app instead of its own separate process.
"""

from __future__ import annotations

from typing import List

from fastapi import APIRouter, Query
from pydantic import BaseModel

from . import common

router = APIRouter()

AVERAGE_BLOCK_SPEED_KMH = 700.0
TURNAROUND_OVERHEAD_HOURS = 1.0
SHORT_HAUL_BASE_FEE_EUR = 40.0
SHORT_HAUL_RATE_PER_KM = 0.10
LONG_HAUL_THRESHOLD_KM = 3500.0
LONG_HAUL_BASE_FEE_EUR = 150.0
LONG_HAUL_RATE_PER_KM = 0.06

_ROUTES: List[dict] = common.load_json("flight_routes.json")
_ROUTES_INDEX = {}
for _r in _ROUTES:
    _ROUTES_INDEX[(_r["origin"].strip().lower(), _r["destination"].strip().lower())] = _r
    _ROUTES_INDEX[(_r["destination"].strip().lower(), _r["origin"].strip().lower())] = _r


class FlightEstimate(BaseModel):
    origin: str
    destination: str
    distance_km: float
    duration_hours: float
    price: float
    currency: str = "EUR"
    source: str  # "curated_table" | "distance_estimate"


@router.get("/flights/estimate", response_model=FlightEstimate)
def estimate_flight(
    origin_name: str = Query(...),
    origin_lat: float = Query(...),
    origin_lon: float = Query(...),
    destination_name: str = Query(...),
    destination_lat: float = Query(...),
    destination_lon: float = Query(...),
):
    key = (origin_name.strip().lower(), destination_name.strip().lower())
    curated = _ROUTES_INDEX.get(key)
    distance_km = common.haversine_km(origin_lat, origin_lon, destination_lat, destination_lon)

    if curated:
        return FlightEstimate(
            origin=origin_name, destination=destination_name,
            distance_km=round(distance_km, 1), duration_hours=curated["duration_hours"],
            price=curated["price_eur"], currency="EUR", source="curated_table",
        )

    if distance_km > LONG_HAUL_THRESHOLD_KM:
        base_fee, rate = LONG_HAUL_BASE_FEE_EUR, LONG_HAUL_RATE_PER_KM
    else:
        base_fee, rate = SHORT_HAUL_BASE_FEE_EUR, SHORT_HAUL_RATE_PER_KM

    duration_hours = TURNAROUND_OVERHEAD_HOURS + distance_km / AVERAGE_BLOCK_SPEED_KMH
    price = base_fee + rate * distance_km

    return FlightEstimate(
        origin=origin_name, destination=destination_name,
        distance_km=round(distance_km, 1), duration_hours=round(duration_hours, 2),
        price=round(price / 5) * 5, currency="EUR", source="distance_estimate",
    )
