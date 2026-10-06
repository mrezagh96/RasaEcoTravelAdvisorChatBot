"""
data_services/carbon.py
=====================================================================
Carbon footprint by transport mode. This one was ALREADY fully
self-hosted (see 08_carbon_estimate.py) — no external API in the
default path at all. Formalised as a router here for consistency with
every other domain now living behind this one service. Climatiq stays
available as an optional, explicitly opt-in upgrade via CLIMATIQ_API_KEY.
"""

from __future__ import annotations

import os
from typing import List, Optional, Tuple

import requests
from fastapi import APIRouter, Query
from pydantic import BaseModel

from . import common

router = APIRouter()

CLIMATIQ_API_KEY = os.environ.get("CLIMATIQ_API_KEY", "")

# Indicative gCO2e per passenger-km. Cited sources: UK DESNZ/DEFRA GHG
# conversion factors and the EEA transport emissions dataset — see
# README "Data sources" for the full citation.
EMISSION_FACTORS = {
    "walk": 0, "cycle": 0, "train": 35, "coach": 27,
    "electric_car": 50, "car_petrol": 170, "ferry": 115,
    "flight_short": 250, "flight_long": 150,
}


class ModeEstimate(BaseModel):
    mode: str
    carbon_kg: float


class CarbonResult(BaseModel):
    distance_km: float
    modes: List[ModeEstimate]
    source: str  # "local_table" | "climatiq"


def _estimate_kg(mode: str, distance_km: float) -> float:
    return round(EMISSION_FACTORS.get(mode, 0) * distance_km / 1000.0, 2)


def _climatiq_estimate(distance_km: float) -> Optional[Tuple[float, str]]:
    if not CLIMATIQ_API_KEY:
        return None
    response = requests.post(
        "https://api.climatiq.io/data/v1/estimate",
        headers={**common.HEADERS, "Authorization": f"Bearer {CLIMATIQ_API_KEY}"},
        json={
            "emission_factor": {
                "activity_id": "passenger_vehicle-vehicle_type_car-fuel_source_na-engine_size_na-vehicle_age_na-vehicle_weight_na",
                "data_version": "^6",
            },
            "parameters": {"distance": distance_km, "distance_unit": "km"},
        },
        timeout=8,
    )
    response.raise_for_status()
    data = response.json()
    return data["co2e"], data["co2e_unit"]


@router.get("/carbon/compare", response_model=CarbonResult)
def compare_modes(distance_km: float = Query(...), modes: str = Query("train,coach,car_petrol,flight_short")):
    mode_list = [m.strip() for m in modes.split(",") if m.strip()]
    estimates = sorted(
        (ModeEstimate(mode=m, carbon_kg=_estimate_kg(m, distance_km)) for m in mode_list),
        key=lambda e: e.carbon_kg,
    )
    return CarbonResult(distance_km=distance_km, modes=estimates, source="local_table")
