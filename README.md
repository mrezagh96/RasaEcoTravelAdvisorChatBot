---
title: Eco Travel Advisor
emoji: 🧭
colorFrom: green
colorTo: yellow
sdk: docker
app_port: 7860
pinned: false
license: mit
---

# 🧭 Eco-Travel Advisor

A Rasa-based conversational agent that plans lower-carbon trips: tell it
where you're going, and it geocodes both cities, estimates flights, finds
hotels and public transport near your destination, checks the weather,
converts currencies, and ranks everything with a weighted carbon/price
scoring function tuned to how much sustainability matters to you.

Built for *Advanced Conversational UI Design & Chatbot Development*
(MSc Artificial Intelligence, BSBI Berlin / UCA).

## Contents

- [Architecture](#architecture)
- [Quick start — local development](#quick-start--local-development)
- [Quick start — Docker Compose](#quick-start--docker-compose)
- [Deploying to HuggingFace Spaces](#deploying-to-huggingface-spaces)
- [Data sources](#data-sources)
- [The fallback & human-handover system](#the-fallback--human-handover-system)
- [Frontend design decisions](#frontend-design-decisions)
- [Known limitations](#known-limitations)
- [Project structure](#project-structure)
- [Environment variables](#environment-variables)

## Architecture

```
                     ┌─────────────────────┐
   user ──────────── │  Streamlit dashboard │  (only public port on HF Spaces)
                     └──────────┬───────────┘
                                │ REST (/webhooks/rest/webhook)
                     ┌──────────▼───────────┐
                     │      Rasa server      │  NLU (DIET) + Core (rules/forms)
                     └──────────┬───────────┘
                                │ action_endpoint
                     ┌──────────▼───────────┐
                     │  Rasa action server   │  actions/*.py
                     └──────────┬───────────┘
                                │ REST — ONE local service for everything
                     ┌──────────▼───────────┐
                     │     data_services      │  geocode · hotels · transport ·
                     │   FastAPI, :8000       │  attractions · weather · currency ·
                     └──────────┬───────────┘  carbon · flights (8 routers, 1 app)
                                │ live-first, curated-data fallback per domain
                     Nominatim · Overpass · Open-Meteo ·
                     Frankfurter · Wikipedia REST (all free, no key)
```

All four processes run inside **one container** in the HuggingFace Spaces
deployment (`Dockerfile` + `start.sh`), since Spaces only routes external
traffic to a single port. `docker-compose.yml` runs the same four pieces as
properly separated containers for local development.

`data_services/` is deliberately ONE FastAPI app with one router per
domain, not eight separate services — see its `main.py` module
docstring. That keeps the terminal count exactly what it was before
(one process for "all the data stuff") while still letting
`actions/api_clients.py` be a single, uniform REST client instead of
juggling five different external domains directly.

## Quick start — local development

Same two-terminal pattern as Worksheet 1/2, plus a third terminal for
the data services:

```bash
python3.10 -m venv .venv
source .venv/bin/activate          # .venv\Scripts\activate on Windows
pip install -r requirements.txt

rasa data validate                 # catches domain/data mistakes in seconds
rasa train

# terminal 1
uvicorn data_services.main:app --port 8000

# terminal 2
rasa run actions --port 5055

# terminal 3
rasa run --enable-api --cors "*" --port 5005

# terminal 4
streamlit run streamlit_app.py
```

Python **3.10** specifically — `rasa==3.6.21` requires `>=3.8,<3.11` (see
`requirements.txt` for the full reasoning behind every pinned version).

## Quick start — Docker Compose

```bash
docker compose up --build
```

Then open `http://localhost:8501`. Rasa's REST API is on `:5005`, the
action server on `:5055`, data_services on `:8000`.

## Deploying to HuggingFace Spaces

1. Create a new Space → **Docker** SDK.
2. Push this whole repository to the Space (the YAML block at the top of
   this file is the Space's required metadata — `sdk: docker` and
   `app_port: 7860` are what make it "just work" with no extra
   configuration in the Spaces UI).
3. Wait for the build. **The first build takes several minutes** — it
   installs Rasa's full dependency stack and then runs `rasa train`
   *during the image build* (see `Dockerfile`) so the container itself
   starts in seconds on every subsequent boot/wake. This is normal, not
   a hang.
4. If the build fails at the `rasa data validate` step, that means a
   genuine domain/data inconsistency slipped in — the log will say
   exactly which intent/action/slot is the problem.

No secrets are required for the default setup. `CLIMATIQ_API_KEY` is
optional (see Environment variables) — without it, carbon figures come
entirely from the local emission-factor table, which is a fully
documented, citable fallback, not a degraded mode.

## Data sources

Every domain below lives behind `data_services/` (one FastAPI app, one
router per domain — see its `main.py`). Each router tries the real,
live public API first; if that fails, it falls back to its own curated
data in `data_services/data/*.json` rather than a dead end. Every
response carries a `source` field (`"live"`, `"curated"`,
`"cached_fallback"`, `"seasonal_estimate"`, ...) so it's always
possible to tell which path answered.

| Data | Live source | Curated fallback | Notes |
|---|---|---|---|
| Geocoding | Nominatim (OpenStreetMap) | ~170 major world cities, real coordinates (`geocode.py`) | Common destinations resolve instantly with no network call at all |
| Hotels | Overpass (OpenStreetMap) | Generic, clearly-labelled illustrative listings (`hotels.py`) | Real hotel names are never fabricated — the fallback says "illustrative example, not a real listing" rather than inventing a business |
| Public transport | Overpass (OpenStreetMap) | Curated transit-quality rating for ~20 major cities (`transport.py`) | Ratings are general, well-known facts (e.g. "Tokyo's metro is extensive"), not invented statistics |
| Attractions | Overpass + Wikipedia REST | Real, well-known landmarks for ~17 major cities (`attractions.py`) | Curated entries are genuine facts (Eiffel Tower, Colosseum, ...); uncurated cities get an honest generic note instead of a made-up landmark |
| Weather | Open-Meteo | Seasonal average by latitude band + month (`weather.py`) | The fallback is explicitly labelled an estimate, never presented as a live forecast |
| Currency | Frankfurter (ECB reference rates) | Cached approximate rates for ~20 currencies (`currency.py`) | Volatile currencies (e.g. IRR, TRY) are deliberately left out of the fallback rather than publish a badly-stale number |
| Carbon (transport modes) | — (was always self-hosted) | Local factor table, cited from UK DESNZ/DEFRA & EEA conversion factors (`carbon.py`) | Optional Climatiq API for real-time figures if `CLIMATIQ_API_KEY` is set |
| Flights | — (no viable free live API — see below) | Curated table of ~50 common routes + distance-based formula (`flights.py`) | See below |

**Why flights have no live source:** the brief names the Amadeus for
Developers sandbox API. Amadeus's self-service platform was decommissioned
on 17 July 2026 — the portal and existing keys stopped working. As of this
project's build date, no free, no-signup, no-payment-details API returns
live flight prices; the closest legitimate alternatives
(Travelpayouts/Aviasales Data API, Kiwi Tequila) require partner approval
that can't be guaranteed before a coursework deadline, and a cluster of
newer "AI-agent flight booking" tools ask for a real payment card via
Stripe, which is inappropriate for a student project. `data_services/flights.py`
answers from a curated table of ~50 common routes with indicative
price/duration, and a documented haversine-distance fallback formula
(constants declared and justified in the file itself) for any route not
in the table.

## The fallback & human-handover system

Custom-built rather than Rasa's default two-stage fallback (which only has
two stages) to match this project's spec exactly — see
`actions/fallback.py`:

1. **1st & 2nd** time the bot doesn't understand, or the user asks
   something off-topic → a nudge back to the bot's actual job, with
   quick-reply buttons generated in Python (not hardcoded in the UI).
2. **3rd** consecutive time in the same detour → a graceful decline
   ("I don't have information about that"), then the streak resets so a
   *later, separate* detour starts counting from zero again.
3. That decline is capped at **5 uses across the whole conversation**
   (`fallback_total_giveups`). On the 6th time the bot would have to
   decline again, it escalates to `action_handover_to_human` instead.

Human handover packages the full conversation (collected slots, last
20 turns of transcript, last intent/confidence) and — since this project
has no real ticketing system to send it to — logs it to
`handover_queue.jsonl` and prints it to the action server's console. That
substitution is documented here and in the code on purpose: describing an
integration you didn't build is fine for a coursework report; presenting a
console log as a finished integration would not be.

## Frontend design decisions

Palette and type were chosen to avoid the generic "AI dashboard" defaults
(warm cream + terracotta; SaaS card grids with identical shadows) in favour
of something grounded in the subject matter — a physical travel document:

| Token | Value | Role |
|---|---|---|
| Background | `#1B2B22` | deep pine charcoal |
| Surface | `#24352A` | cards / panels |
| Text | `#EDEBE2` | warm paper |
| Accent | `#C9A24B` | compass gold |
| Tier green/amber/red | `#5FA777` / `#D9A441` / `#C6553D` | the one required colour-coding signal |

Headings use **Fraunces** (a characterful serif, evoking old travel
posters); body/UI text uses **Inter** for density and legibility in chat.
Result cards are styled as **boarding passes / luggage tags** — an
asymmetric card with a dashed perforation and a rotated stub label —
rather than identical rounded SaaS cards, because the content genuinely is
a travel document. The tier badge is the *one* bold, saturated element;
everything else stays quiet charcoal-on-paper so the colour-coding the
brief asks for actually stands out instead of competing with decoration.

## Known limitations

Documented here so they can be cited honestly in the report rather than
discovered by a marker:

- **Hotel nightly price is estimated from star rating**, not a real quote
  (OSM carries almost no price data — see `02_find_hotels.py`'s own
  note). Always labelled `price_is_estimated: true` in the data sent to
  the frontend.
- **Stay length is fixed at 3 nights** (`NIGHTS_PER_STAY` in
  `actions/actions.py`) rather than computed from the two dates the user
  gives, to keep the scope of this iteration manageable.
- **No free fare API exists for rail/coach** across Europe, so those
  travel options carry a carbon figure but no price — the UI discloses
  this rather than inventing a number.
- **Booking is a mock confirmation only.** No payment or real reservation
  API is integrated — this is disclosed to the user in the confirmation
  message itself, not just in this file.
- **Tracker store is the default in-memory store** (see `endpoints.yml`).
  Fine for a demo; a persistent store (SQL/Redis) would be the production
  upgrade path.
- **City-name entity recognition** relies on DIET generalising from a
  training set of major cities — an unusual or very small town's name may
  not be extracted correctly. `validate_origin_city` /
  `validate_destination_city` catch this by geocoding whatever *was*
  extracted and asking again if Nominatim can't find it, but if DIET
  extracts nothing at all, the form will just re-ask.

## Project structure

```
eco-travel-advisor/
├── config.yml                  # NLU pipeline + dialogue policies
├── domain.yml                  # intents, entities, slots, responses, forms
├── endpoints.yml
├── data/
│   ├── nlu.yml
│   ├── rules.yml
│   └── stories.yml
├── actions/
│   ├── actions.py              # form validation + trip recommendation orchestrator
│   ├── api_clients.py          # thin REST client to data_services (+ 2 pure-local helpers)
│   ├── scoring.py               # weighted carbon/price ranking function
│   └── fallback.py              # smart fallback (3-nudge/5-giveup) + human handover
├── data_services/                # ONE FastAPI app — every external data need
│   ├── main.py                   # mounts all 8 routers, single entry point
│   ├── common.py                 # shared retry/cache/haversine helpers
│   ├── geocode.py / hotels.py / transport.py / attractions.py /
│   │   weather.py / currency.py / carbon.py / flights.py   # one router per domain
│   └── data/                     # curated fallback datasets (cities, attractions, ...)
├── streamlit_app.py             # dashboard frontend
├── Dockerfile                   # single-container build (HuggingFace Spaces)
├── docker-compose.yml           # four-container local dev topology
├── start.sh                     # boots all four processes in the combined container
└── requirements.txt
```

## Environment variables

| Variable | Default | Used by |
|---|---|---|
| `RASA_URL` | `http://localhost:5005` | `streamlit_app.py` |
| `DATA_SERVICES_URL` | `http://localhost:8000` | `actions/api_clients.py` |
| `CLIMATIQ_API_KEY` | *(unset)* | `data_services/carbon.py` — optional, falls back to the local carbon table |
| `HANDOVER_LOG_PATH` | `handover_queue.jsonl` | `actions/fallback.py` |
| `PORT` | `7860` | `start.sh` — the single port HuggingFace Spaces routes to |

`endpoints.yml`'s `action_endpoint.url` is hardcoded to `http://localhost:5055/webhook`
rather than driven by an env var — Rasa's YAML loader doesn't expand `${VAR}`
shell substitutions, so an env var there would silently never take effect.
`localhost` is correct for plain local dev and the combined HF Spaces
container; `docker-compose.yml`'s `rasa` service rewrites this file with
`sed` at startup for the multi-container case (see that file's comments).
