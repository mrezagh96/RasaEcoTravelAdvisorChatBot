"""
data_services/currency.py
=====================================================================
Currency conversion. Frankfurter/ECB (see 07_currency_exchange.py) is
tried first — it's free, reliable and needs no key. If it's ever
unreachable, falls back to data/currency_fallback.json, a small table
of rounded, clearly-stale approximate rates against EUR. Every
response says which source it came from; the fallback path refuses
to answer for currency pairs it doesn't have rather than guessing.
"""

from __future__ import annotations

from typing import Optional

import requests
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from . import common

router = APIRouter()

FRANKFURTER_URL = "https://api.frankfurter.dev/v1"
_FALLBACK = common.load_json("currency_fallback.json")


class ConversionResult(BaseModel):
    amount: float
    from_currency: str
    to_currency: str
    converted: float
    rate: float
    date: str
    source: str  # "live" | "cached_fallback"


def _live_rate(from_currency: str, to_currency: str) -> Optional[dict]:
    response = requests.get(
        f"{FRANKFURTER_URL}/latest",
        params={"base": from_currency.upper(), "symbols": to_currency.upper()},
        headers=common.HEADERS,
        timeout=8,
    )
    response.raise_for_status()
    data = response.json()
    if to_currency.upper() not in data.get("rates", {}):
        return None
    return {"rate": data["rates"][to_currency.upper()], "date": data["date"]}


def _fallback_rate(from_currency: str, to_currency: str) -> Optional[float]:
    from_c, to_c = from_currency.upper(), to_currency.upper()
    rates = _FALLBACK["rates"]
    if from_c == to_c:
        return 1.0
    if from_c == "EUR" and to_c in rates:
        return rates[to_c]
    if to_c == "EUR" and from_c in rates:
        return 1 / rates[from_c]
    if from_c in rates and to_c in rates:
        return rates[to_c] / rates[from_c]  # via EUR
    return None


@router.get("/currency/convert", response_model=ConversionResult)
def convert(amount: float = Query(1.0), from_currency: str = Query(...), to_currency: str = Query(...)):
    live = common.try_live(_live_rate, from_currency, to_currency)
    if live:
        return ConversionResult(
            amount=amount, from_currency=from_currency.upper(), to_currency=to_currency.upper(),
            converted=round(amount * live["rate"], 2), rate=live["rate"], date=live["date"], source="live",
        )

    rate = _fallback_rate(from_currency, to_currency)
    if rate is not None:
        return ConversionResult(
            amount=amount, from_currency=from_currency.upper(), to_currency=to_currency.upper(),
            converted=round(amount * rate, 2), rate=rate, date="cached (approximate)", source="cached_fallback",
        )

    raise HTTPException(status_code=502, detail=f"No live or cached rate available for {from_currency} -> {to_currency}")
