"""
data_services/weather.py
=====================================================================
Weather is inherently live/dynamic — there's no sensible way to
"curate" today's forecast the way flight routes or city coordinates
can be curated. Open-Meteo (see 06_weather_forecast.py) is tried
first. If it's unreachable, the fallback is a general, honestly-
labelled SEASONAL AVERAGE derived from latitude band + month (basic
climate-zone knowledge: how far from the equator, and which
hemisphere's summer/winter it is for a given month) — never presented
as an actual forecast, always flagged as an estimate.

`/weather/forecast` is the per-day endpoint (trip summaries showing a
temperature for each day of the stay; the Weather Checking flow's
resolved "which day?" answer). Open-Meteo can only forecast ~16 days
out from TODAY — regardless of how far in the future the trip itself
is — so each requested day is answered independently: live if it falls
within that live window, the seasonal estimate for that day's month
otherwise. A single live Open-Meteo call covers the whole live window
in one request no matter how many days are requested.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import List, Optional

import requests
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from . import common

router = APIRouter()

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
LIVE_FORECAST_HORIZON_DAYS = 16  # Open-Meteo's practical free-tier forecast window


class WeatherResult(BaseModel):
    temperature_c: float
    precipitation_mm: float
    wind_kmh: Optional[float] = None
    advice: str
    source: str  # "live" | "seasonal_estimate"


class DayWeather(BaseModel):
    date: str  # YYYY-MM-DD
    temperature_max_c: float
    precipitation_mm: float
    advice: str
    source: str  # "live" | "seasonal_estimate"


class ForecastResult(BaseModel):
    days: List[DayWeather]


def _live_weather(lat: float, lon: float) -> Optional[dict]:
    response = requests.get(
        OPEN_METEO_URL,
        params={
            "latitude": lat, "longitude": lon,
            "current": "temperature_2m,precipitation,wind_speed_10m",
            "forecast_days": 1, "timezone": "auto",
        },
        headers=common.HEADERS,
        timeout=8,
    )
    response.raise_for_status()
    current = response.json()["current"]
    return {
        "temperature_c": current["temperature_2m"],
        "precipitation_mm": current["precipitation"],
        "wind_kmh": current["wind_speed_10m"],
    }


def _live_daily_weather(lat: float, lon: float) -> Optional[dict]:
    """Up to LIVE_FORECAST_HORIZON_DAYS of real forecast, keyed by
    ISO date string, from ONE Open-Meteo call — reused for however
    many days of a trip actually fall within that window."""
    response = requests.get(
        OPEN_METEO_URL,
        params={
            "latitude": lat, "longitude": lon,
            "daily": "temperature_2m_max,precipitation_sum",
            "forecast_days": LIVE_FORECAST_HORIZON_DAYS,
            "timezone": "auto",
        },
        headers=common.HEADERS,
        timeout=8,
    )
    response.raise_for_status()
    daily = response.json()["daily"]
    by_date = {}
    for i, iso_date in enumerate(daily["time"]):
        by_date[iso_date] = {
            "temperature_max_c": daily["temperature_2m_max"][i],
            "precipitation_mm": daily["precipitation_sum"][i],
        }
    return by_date


def _seasonal_estimate_for_month(lat: float, month: int) -> dict:
    """Rough climate-zone estimate for a given calendar month (1-12) —
    parameterised by month rather than "now" so it works for a trip
    booked weeks or months in the future, not just today."""
    effective_month = month if lat >= 0 else ((month + 6 - 1) % 12) + 1
    season_curve = {1: 0.0, 2: 0.1, 3: 0.3, 4: 0.5, 5: 0.8, 6: 1.0, 7: 1.0, 8: 0.9, 9: 0.6, 10: 0.4, 11: 0.2, 12: 0.0}
    warmth = season_curve[effective_month]

    abs_lat = abs(lat)
    if abs_lat < 23.5:
        cold_temp, warm_temp = 24, 30
    elif abs_lat < 40:
        cold_temp, warm_temp = 10, 30
    elif abs_lat < 55:
        cold_temp, warm_temp = 2, 24
    else:
        cold_temp, warm_temp = -8, 17

    temp_c = round(cold_temp + (warm_temp - cold_temp) * warmth, 1)
    return {"temperature_c": temp_c, "precipitation_mm": 0.0}


def _travel_advice(temp_c: float, rain_mm: float) -> str:
    if rain_mm > 5:
        return "wet — plan indoor activities, and public transport over cycling"
    if temp_c < 5:
        return "cold — walking tours will be hard going"
    if temp_c > 30:
        return "hot — cycling and long walks are unwise in the middle of the day"
    return "good conditions for walking and cycling"


@router.get("/weather", response_model=WeatherResult)
def get_weather(lat: float = Query(...), lon: float = Query(...)):
    live = common.try_live(_live_weather, lat, lon)
    if live:
        advice = _travel_advice(live["temperature_c"], live["precipitation_mm"])
        return WeatherResult(**live, advice=advice, source="live")

    estimate = _seasonal_estimate_for_month(lat, datetime.now(timezone.utc).month)
    advice = _travel_advice(estimate["temperature_c"], estimate["precipitation_mm"]) + " (seasonal estimate, not a live forecast)"
    return WeatherResult(**estimate, wind_kmh=None, advice=advice, source="seasonal_estimate")


@router.get("/weather/forecast", response_model=ForecastResult)
def get_forecast(
    lat: float = Query(...),
    lon: float = Query(...),
    start_date: str = Query(...),
    num_days: int = Query(1, ge=1, le=30),
):
    try:
        start = date.fromisoformat(start_date)
    except ValueError:
        raise HTTPException(status_code=400, detail="start_date must be YYYY-MM-DD")

    requested_dates = [start + timedelta(days=i) for i in range(num_days)]
    live_by_date = common.try_live(_live_daily_weather, lat, lon) or {}

    days: List[DayWeather] = []
    for d in requested_dates:
        key = d.isoformat()
        if key in live_by_date:
            entry = live_by_date[key]
            advice = _travel_advice(entry["temperature_max_c"], entry["precipitation_mm"])
            days.append(DayWeather(date=key, temperature_max_c=entry["temperature_max_c"],
                                    precipitation_mm=entry["precipitation_mm"], advice=advice, source="live"))
        else:
            est = _seasonal_estimate_for_month(lat, d.month)
            advice = _travel_advice(est["temperature_c"], est["precipitation_mm"]) + " (seasonal estimate)"
            days.append(DayWeather(date=key, temperature_max_c=est["temperature_c"],
                                    precipitation_mm=est["precipitation_mm"], advice=advice, source="seasonal_estimate"))

    return ForecastResult(days=days)
