"""
data_services/main.py — Eco-Travel Advisor's unified data layer
=====================================================================
ONE FastAPI app, ONE process, ONE port (8000), covering every external
data need the bot has: geocoding, hotels, transport, attractions,
weather, currency, carbon, and flights. Each domain lives in its own
router module (geocode.py, hotels.py, ...) for readability, but they
all mount here so starting the whole data layer is a single command:

    uvicorn data_services.main:app --port 8000

This replaces both the direct external-API calls that used to live in
actions/api_clients.py AND the separate flight_data_service/ process —
actions.py now talks to exactly one local service for everything,
which is what makes the reliability work (retry/cache/fallback logic
lives in ONE place per domain, see common.py) and the terminal count
manageable (this is still just "one more terminal", not eight).

Every route tries the real, live public API first (short timeout, one
retry) and falls back to this package's own curated data
(data_services/data/*.json) when that fails — see each router's
module docstring for what its specific fallback is and why it's
trustworthy rather than invented. Every response includes a `source`
field so a caller (or a marker reading the report) can always tell
curated/cached data apart from a live call.
"""

from fastapi import FastAPI

from . import attractions, carbon, currency, flights, geocode, hotels, transport, weather

app = FastAPI(
    title="Eco-Travel Advisor — Data Services",
    description="Unified, resilient data layer: geocoding, hotels, transport, attractions, weather, currency, carbon, flights.",
    version="1.0.0",
)

app.include_router(geocode.router, tags=["geocode"])
app.include_router(hotels.router, tags=["hotels"])
app.include_router(transport.router, tags=["transport"])
app.include_router(attractions.router, tags=["attractions"])
app.include_router(weather.router, tags=["weather"])
app.include_router(currency.router, tags=["currency"])
app.include_router(carbon.router, tags=["carbon"])
app.include_router(flights.router, tags=["flights"])


@app.get("/health")
def health():
    return {
        "status": "ok",
        "services": ["geocode", "hotels", "transport", "attractions", "weather", "currency", "carbon", "flights"],
    }
