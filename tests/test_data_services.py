"""
tests/test_data_services.py — smoke tests for the unified data_services app
=====================================================================
Run with:  pytest tests/ -v
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from data_services.main import app

client = TestClient(app)


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert "flights" in response.json()["services"]


class TestGeocode:
    def test_curated_city_resolves_instantly(self):
        r = client.get("/geocode", params={"place": "Tabriz"})
        assert r.status_code == 200
        data = r.json()
        assert data["source"] == "curated"
        assert 37 < data["lat"] < 39  # Tabriz is around 38.08 N

    def test_curated_lookup_is_case_insensitive(self):
        r = client.get("/geocode", params={"place": "BERLIN"})
        assert r.status_code == 200
        assert r.json()["source"] == "curated"

    def test_unknown_place_with_no_network_returns_404(self):
        r = client.get("/geocode", params={"place": "Definitely-Not-A-Real-Place-XYZ"})
        assert r.status_code == 404


class TestHotels:
    def test_hotels_response_is_well_formed_live_or_fallback(self):
        # Whether this hits real Overpass data (when the test runner has
        # internet access) or the fallback depends on network conditions
        # at test time — both are correct outcomes.
        r = client.get("/hotels", params={"lat": 38.08, "lon": 46.29, "city_name": "Tabriz"})
        assert r.status_code == 200
        data = r.json()
        assert data["source"] in ("live", "fallback")
        assert len(data["hotels"]) > 0
        if data["source"] == "fallback":
            # Fallback names are generic "<City> ... Hotel" placeholders.
            assert all("Tabriz" in h["name"] for h in data["hotels"])
        else:
            assert all(not h["is_illustrative"] for h in data["hotels"])


class TestTransport:
    def test_rating_for_major_city_live_or_curated(self):
        r = client.get("/transport", params={"lat": 52.52, "lon": 13.405, "city_name": "Berlin"})
        assert r.status_code == 200
        data = r.json()
        assert data["source"] in ("live", "curated")
        assert "excellent" in data["summary"]

    def test_unknown_city_is_honest_about_not_knowing(self):
        # Real Overpass can occasionally return something even for a
        # coordinate this remote — that's still an honest, non-fabricated
        # answer, so "live" is an acceptable outcome here too, not just
        # "unknown". What actually matters (checked below) is that no
        # curated rating gets fabricated for a city we have no entry for.
        r = client.get("/transport", params={"lat": 1.0, "lon": 1.0, "city_name": "NowhereTown"})
        assert r.status_code == 200
        assert r.json()["source"] in ("live", "unknown")


class TestAttractions:
    def test_curated_attractions_for_major_city(self):
        r = client.get("/attractions", params={"lat": 52.52, "lon": 13.405, "city_name": "Berlin"})
        assert r.status_code == 200
        data = r.json()
        assert data["source"] in ("live", "curated")
        if data["source"] == "curated":
            names = [a["name"] for a in data["attractions"]]
            assert "Brandenburg Gate" in names

    def test_uncurated_city_gets_generic_honest_fallback(self):
        r = client.get("/attractions", params={"lat": 1.0, "lon": 1.0, "city_name": "NowhereTown"})
        assert r.status_code == 200
        data = r.json()
        assert data["source"] in ("live", "generic")


class TestWeather:
    def test_weather_response_well_formed_live_or_seasonal(self):
        r = client.get("/weather", params={"lat": 52.52, "lon": 13.405})
        assert r.status_code == 200
        data = r.json()
        assert data["source"] in ("live", "seasonal_estimate")
        assert isinstance(data["temperature_c"], (int, float))

    def test_seasonal_estimate_is_warmer_near_equator(self):
        tropical = client.get("/weather", params={"lat": 1.35, "lon": 103.8}).json()  # Singapore
        temperate = client.get("/weather", params={"lat": 60.17, "lon": 24.9}).json()  # Helsinki
        if tropical["source"] == "seasonal_estimate" and temperate["source"] == "seasonal_estimate":
            assert tropical["temperature_c"] > temperate["temperature_c"]

    def test_forecast_near_term_well_formed(self):
        from datetime import date
        r = client.get("/weather/forecast", params={"lat": 52.52, "lon": 13.405, "start_date": date.today().isoformat(), "num_days": 3})
        assert r.status_code == 200
        days = r.json()["days"]
        assert len(days) == 3
        for d in days:
            assert d["source"] in ("live", "seasonal_estimate")

    def test_forecast_far_future_falls_back_to_seasonal(self):
        from datetime import date, timedelta
        far = (date.today() + timedelta(days=90)).isoformat()
        r = client.get("/weather/forecast", params={"lat": 52.52, "lon": 13.405, "start_date": far, "num_days": 2})
        assert r.status_code == 200
        for d in r.json()["days"]:
            assert d["source"] == "seasonal_estimate"

    def test_forecast_bad_date_returns_400(self):
        r = client.get("/weather/forecast", params={"lat": 52.52, "lon": 13.405, "start_date": "not-a-date", "num_days": 1})
        assert r.status_code == 400


class TestCurrency:
    def test_conversion_well_formed_live_or_fallback(self):
        r = client.get("/currency/convert", params={"amount": 100, "from_currency": "USD", "to_currency": "EUR"})
        assert r.status_code == 200
        data = r.json()
        assert data["source"] in ("live", "cached_fallback")
        assert data["converted"] > 0

    def test_same_currency_is_identity(self):
        r = client.get("/currency/convert", params={"amount": 50, "from_currency": "EUR", "to_currency": "EUR"})
        assert r.json()["converted"] == 50

    def test_unknown_currency_pair_returns_502(self):
        r = client.get("/currency/convert", params={"amount": 1, "from_currency": "XXX", "to_currency": "YYY"})
        assert r.status_code == 502


class TestCarbon:
    def test_compare_modes_ranks_lowest_first(self):
        r = client.get("/carbon/compare", params={"distance_km": 900})
        assert r.status_code == 200
        modes = r.json()["modes"]
        assert modes[0]["carbon_kg"] <= modes[-1]["carbon_kg"]


class TestFlights:
    def test_curated_route(self):
        r = client.get("/flights/estimate", params={
            "origin_name": "Berlin", "origin_lat": 52.52, "origin_lon": 13.405,
            "destination_name": "Lisbon", "destination_lat": 38.7223, "destination_lon": -9.1393,
        })
        assert r.status_code == 200
        assert r.json()["source"] == "curated_table"

    def test_uncurated_route_falls_back_to_distance_formula(self):
        r = client.get("/flights/estimate", params={
            "origin_name": "Berlin", "origin_lat": 52.52, "origin_lon": 13.405,
            "destination_name": "Tabriz", "destination_lat": 38.08, "destination_lon": 46.29,
        })
        assert r.status_code == 200
        data = r.json()
        assert data["source"] == "distance_estimate"
        assert data["price"] > 0


class TestLiveGeocodeCleanName:
    """Regression test for a real, reported bug: a live Nominatim match
    that isn't a clean city (a landmark, building, etc.) was returning
    its full, verbose comma-separated address as the "city name" shown
    to the user (e.g. "Jardin botanique de Genève, 1, Pâquis,
    Chambésy, ... Schweiz/Suisse/Svizzera/Svizra" instead of "Genève").
    Fixed via addressdetails=1 + extracting the structured city/town/
    village field instead of the raw display_name."""

    def test_extracts_clean_city_from_structured_address(self):
        from unittest.mock import MagicMock, patch
        from data_services import geocode

        fake_response = MagicMock()
        fake_response.raise_for_status = lambda: None
        fake_response.json = lambda: [{
            "lat": "46.2088", "lon": "6.1487",
            "display_name": (
                "Jardin botanique de Genève, 1, Pâquis, Chambésy, "
                "Pregny-Chambésy, Genève, 1292, Schweiz/Suisse/Svizzera/Svizra"
            ),
            "address": {
                "attraction": "Jardin botanique de Genève",
                "village": "Pregny-Chambésy",
                "city": "Genève",
                "country": "Schweiz/Suisse/Svizzera/Svizra",
            },
        }]
        with patch("requests.get", return_value=fake_response):
            result = geocode._live_geocode("some garden query")
        assert result["display_name"] == "Genève"

    def test_falls_back_to_first_display_name_component_with_no_structured_fields(self):
        from unittest.mock import MagicMock, patch
        from data_services import geocode

        fake_response = MagicMock()
        fake_response.raise_for_status = lambda: None
        fake_response.json = lambda: [{
            "lat": "1.0", "lon": "1.0",
            "display_name": "Some Weird Place, Somewhere, Nowhere Country",
            "address": {},
        }]
        with patch("requests.get", return_value=fake_response):
            result = geocode._live_geocode("weird query")
        assert result["display_name"] == "Some Weird Place"