---
title: Eco Travel Advisor
emoji: ✦
colorFrom: green
colorTo: yellow
sdk: docker
app_port: 7860
pinned: false
license: mit
---

# ✦ Eco-Travel Advisor

A Rasa-based conversational agent that plans lower-carbon trips: tell it
where you're going, and it geocodes both cities, estimates flights, finds
hotels and public transport near your destination, checks the weather,
converts currencies, and ranks everything with a weighted carbon/price
scoring function tuned to how much sustainability matters to you. In the
conversation itself the bot introduces itself as **"MGH Travel
Chatbot"** (see `domain.yml`'s `utter_greet`/`utter_bot_challenge` and the
Streamlit page title) — "Eco-Travel Advisor" is this project's/repo's own
name, used in this file and the HuggingFace Space metadata above.

Built for *Advanced Conversational UI Design & Chatbot Development*
(MSc Artificial Intelligence, BSBI Berlin / UCA).

## Contents

- [Status](#status)
- [Architecture](#architecture)
- [Quick start — local development](#quick-start--local-development)
- [Docker and Docker Compose (not used for the submission)](#docker-and-docker-compose-not-used-for-the-submission)
- [Hugging Face Spaces (not used)](#hugging-face-spaces-not-used)
- [Security notes](#security-notes)
- [Data sources](#data-sources)
- [Conversational flows — every question, and every way to answer it](#conversational-flows--every-question-and-every-way-to-answer-it)
- [The fallback & human-handover system](#the-fallback--human-handover-system)
- [Frontend design decisions](#frontend-design-decisions)
- [Testing](#testing)
- [Known limitations](#known-limitations)
- [Project structure](#project-structure)
- [Environment variables](#environment-variables)

## Status

State of the repository on **6 October 2026** (the day of the final test runs):

| Part | State |
|---|---|
| Bot (Rasa 3.6.21, 29 trained intents, 14 entities, 6 forms, 71 rules) | Runs locally as four processes. This is how it was developed, tested and demonstrated. |
| Tests | Rasa NLU and Core tests, `rasa data validate`, pytest and hand tests were run on 6 October 2026. Numbers and files: [Testing](#testing) and `results/`. |
| Docker files (`Dockerfile`, `docker-compose.yml`, `start.sh`) | In the repository, **not used and not tested** for the submission. |
| Cloud deployment (AWS, Azure, GCP, Hugging Face Spaces) | **Not done.** The repository on GitHub is the deliverable. |
| API keys / secrets | **None needed.** Nothing secret is stored in the repository. |

## Architecture

```
                     ┌─────────────────────┐
   user ──────────── │  Streamlit dashboard │  (the only user-facing port)
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

| Process | Port | Command (see Quick start) |
|---|---|---|
| `data_services` (FastAPI) | 8000 | `uvicorn data_services.main:app --port 8000` |
| Rasa action server | 5055 | `rasa run actions --port 5055` |
| Rasa server | 5005 | `rasa run --enable-api --cors "*" --port 5005` |
| Streamlit dashboard | 8501 | `streamlit run streamlit_app.py` |

Streamlit only ever calls Rasa's `/webhooks/rest/webhook`. The action
server only ever calls `data_services` (through `actions/api_clients.py`).
Nothing else in the code reaches an outside website.

`data_services/` is deliberately ONE FastAPI app with one router per
domain, not eight separate services — see its `main.py` module
docstring. That keeps the terminal count exactly what it was before
(one process for "all the data stuff") while still letting
`actions/api_clients.py` be a single, uniform REST client instead of
juggling five different external domains directly.

**Sessions.** A session ends after 60 minutes without a message and the
slots carry over into the next session (`session_config` in `domain.yml`).
The tracker store is Rasa's default in-memory store (`endpoints.yml`).

## Quick start — local development

Four processes, four terminals. Python **3.10** specifically —
`rasa==3.6.21` requires `>=3.8,<3.11` (see `requirements.txt` for the
full reasoning behind every pinned version).

```bash
git clone <repository-url>
cd eco-travel-advisor

python3.10 -m venv .venv
source .venv/bin/activate          # .venv\Scripts\activate on Windows
pip install -r requirements.txt
pip install -r requirements-dev.txt   # only needed to run the tests (pytest, httpx)

rasa data validate                 # catches domain/data mistakes in seconds
rasa train                         # writes models/<name>.tar.gz (not in git)

# terminal 1
uvicorn data_services.main:app --port 8000

# terminal 2
rasa run actions --port 5055

# terminal 3  (loading the model takes about a minute and a half)
rasa run --enable-api --cors "*" --port 5005

# terminal 4
streamlit run streamlit_app.py     # then open http://localhost:8501
```

The trained model is **not** in the repository (`models/` is in
`.gitignore`), so `rasa train` is the one step that cannot be skipped after
a fresh clone. No API key, account or `.env` file is needed.

To run the tests afterwards, see [Running everything](#running-everything).

## Docker and Docker Compose (not used for the submission)

The repository contains a `Dockerfile`, a `start.sh` and a
`docker-compose.yml`. They were written for a container deployment, but
**they were not used for the submission and the images were never built or
run**. The only check done is that `docker compose config -q` accepts the
compose file. Treat them as an untested starting point.

- **`Dockerfile`** builds one image (`python:3.10-slim`): it installs
  `requirements.txt`, runs `rasa data validate` and `rasa train` during the
  build, and starts `start.sh`.
- **`start.sh`** starts the four processes inside that one container
  (data service, action server, Rasa, Streamlit) and waits for each one
  before starting the next. Only Streamlit listens on `$PORT` (7860).
- **`docker-compose.yml`** starts the four processes as four containers
  built from the same image: `docker compose up --build`, then open
  `http://localhost:8501` after about two minutes (Rasa needs that long to
  load the model; Streamlit shows a connection error until then). Only
  port 8501 is published to the network. Ports 8000, 5055 and 5005 are bound
  to `127.0.0.1`, so they can be tested with `curl` from the same computer
  and nobody else can reach them.

There is no `.dockerignore` file. Before building an image, add one that
excludes at least `.venv/`, `models/`, `.git/` and `results/`; otherwise
`COPY . .` copies them into the image.

## Hugging Face Spaces (not used)

The YAML block at the top of this file (`sdk: docker`, `app_port: 7860`) is
the metadata a Docker Space reads. The brief recommends Hugging Face Spaces
(Docker SDK) as the zero-cost option. When this project was finished, the
Hugging Face documentation said that *"Gradio and Docker Spaces run on
compute and require a paid plan to create"* (Spaces Overview, checked on
6 October 2026: <https://huggingface.co/docs/hub/spaces-overview>), so the
bot was **not deployed there**. If you have a plan that allows it, the steps
would be:

1. Create a new Space → **Docker** SDK.
2. Push this repository to the Space.
3. Wait for the build (several minutes: it installs Rasa's dependencies and
   trains the model during the image build).
4. If the build fails at `rasa data validate`, the log names the intent,
   action or slot that is inconsistent.

No secrets are required. The Space metadata is kept in this file only so
that the repository stays ready for this step.

## Security notes

Secure endpoint handling for the way the bot is actually run (four local
processes):

| Process | Port | Who can reach it in a local run | In Docker Compose |
|---|---|---|---|
| `data_services` | 8000 | this computer only (uvicorn binds to `127.0.0.1` by default) | `127.0.0.1` only |
| Rasa action server | 5055 | every network interface (`rasa run actions` has no option to change this) | `127.0.0.1` only |
| Rasa server | 5005 | every network interface (`rasa run` default `--interface 0.0.0.0`) | `127.0.0.1` only |
| Streamlit | 8501 | every network interface | published (the user entry) |

- **No secrets.** No API key is needed, there is no `credentials.yml`, and
  `.env` files are in `.gitignore`. The only variables the code reads are
  listed in [Environment variables](#environment-variables).
- **Open Rasa API.** Rasa is started with `--enable-api --cors "*"` and
  **without** `--auth-token`. That is fine on a personal computer behind a
  router, but not for a public server. Rasa's own documentation says not to
  expose the Rasa server directly and to secure it with a firewall or an
  authentication method
  (<https://legacy-docs-oss.rasa.com/docs/rasa/http-api>). `rasa run` has
  a `--auth-token` option and an `--interface` option (for example
  `--interface 127.0.0.1`); neither was tested with this Streamlit page.
- **CORS.** Streamlit calls Rasa from its own Python process (`requests`),
  not from the browser, so the browser never talks to Rasa. `--cors "*"` is
  kept from the original start commands; narrowing it was not tested.
- **Action server.** It accepts unauthenticated requests. Keep port 5055
  off any public network (a firewall rule, or the `127.0.0.1` binding in the
  compose file).
- **User data.** There is no database. Conversations live in memory and
  disappear when Rasa restarts. A human-handover request is printed to the
  action server's console and never written to disk. The public data APIs
  only receive place names, coordinates and currency codes, never the chat.
- **No privacy notice** is shown in the chat (see Known limitations).

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
| Hotels | Overpass (OpenStreetMap) | Generic placeholder listings such as "<City> Central Hotel" (`hotels.py`) | Fallback names are generic "<City> + Hotel/Inn/Lodge" placeholders, not real businesses |
| Public transport | Overpass (OpenStreetMap) | Curated transit-quality rating for ~20 major cities (`transport.py`) | Ratings are general, well-known facts (e.g. "Tokyo's metro is extensive"), not invented statistics. No timetables |
| Attractions | Overpass + Wikipedia REST | Real, well-known landmarks for ~17 major cities (`attractions.py`) | Curated entries are genuine facts (Eiffel Tower, Colosseum, ...); uncurated cities get an honest generic note instead of a made-up landmark |
| Weather | Open-Meteo | Seasonal average by latitude band + month (`weather.py`) | The fallback is explicitly labelled an estimate, never presented as a live forecast |
| Currency | Frankfurter (ECB reference rates), `/v1` endpoint | Cached approximate rates for ~20 currencies (`currency.py`) | Volatile currencies (e.g. IRR, TRY) are deliberately left out of the fallback rather than publish a badly-stale number. Frankfurter lists its `/v1` API as deprecated but available indefinitely (checked 6 October 2026); the saved rates answer if it ever stops |
| Carbon (transport modes) | — (always self-hosted) | Local factor table, cited from UK DESNZ/DEFRA & EEA conversion factors (`carbon.py`, mirrored locally in `actions/api_clients.py`'s `EMISSION_FACTORS`) | `carbon.py` has an unused `_climatiq_estimate` helper (dead code: `compare_modes()` never calls it, with or without `CLIMATIQ_API_KEY` set), so carbon is self-hosted only |
| Flights | — (no viable free live API — see below) | Curated table of ~50 common routes + distance-based formula (`flights.py`) | See below |
| Train / coach fares | — (no free fare API exists — see "Known limitations") | Distance-based estimate formula (`actions/api_clients.py`'s `estimate_surface_fare_eur`): coach 8.00 EUR + 0.045 EUR per km, train 12.00 EUR + 0.11 EUR per km, rounded to 5 EUR | A disclosed base-fee + per-km rate, not a real quote — see that section |

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
in the table. The same straight-line (haversine) distance is used for the
rail, coach and car estimates, because no routing API key was provided.

## Conversational flows — every question, and every way to answer it

Every question the bot asks can always be answered **two ways**: by
clicking one of the buttons shown with it (a Rasa *structured payload*,
e.g. `/inform_dates{"weather_day_text": "tomorrow"}`), or by typing free
text in the message box. Free text is never a second-class path — it's
handled by that slot's own `extract_<slot>`/`validate_<slot>` logic in
`actions/actions.py`, independently of whether a button exists at all.
A handful of slots (`travel_date_to`, `weather_city`, `exchange_amount`,
`contact_info`, `hotel_list_city`, `to_currency`) have **no buttons at
all** and are answered purely by typing.

Where a typed answer is known not to work as described, the table says so
and points to [Known limitations](#known-limitations).

Two mechanisms apply across almost every flow below and are only
described once here:

- **City fuzzy-matching.** Every city-name slot (`destination_city`,
  `origin_city`, `weather_city`, `hotel_list_city`) is checked against
  `data_services`' city list via `api_clients.match_city()`. An exact or
  confident match is accepted immediately. A close-but-imperfect match
  (a typo, or unusual capitalisation) triggers `"Is this what you mean?
  **<City>**"` with a **Yes** button (payload `/affirm_city_suggestion`);
  that Yes can also be typed instead — `"yes"`, `"yeah"`, `"yep"`,
  `"sure"`, `"correct"`, `"ok"`, `"exactly"`, `"that's it"`, `"sounds
  good"`, and several other phrasings all resolve the suggestion the same
  way (see `_AFFIRMATIVE_WORDS`/`_AFFIRMATIVE_PHRASES` in `actions.py`).
  Typing a different city instead of confirming works too. No match at
  all gets an honest "I couldn't find a place called '...'" and an
  unlimited number of retries.
- **Entry-point gating.** Every flow-starting message funnels through
  `action_route_entry_point` first. If the bot is still waiting on you to
  press **Finish / main menu** after a previous flow finished
  (`awaiting_finish` slot), it reprints that reminder instead of starting
  a new flow — click **Finish / main menu** (or type `/finish`) to clear
  that gate, then start the next flow.

### Trip Assistant & Booking — `trip_planning_form` (Q1–Q8)

**Entry:** the "🧳 Trip Assistant & Booking" button (sidebar, always
visible, or the home-screen grid), or typing something that matches the
`ask_trip_planning` intent (e.g. "I want to plan a trip", "help me book a
trip").

| # | Slot | Question | Buttons | Free-text alternatives |
|---|---|---|---|---|
| Q1 | `destination_city` | "Where would you like to travel to? (e.g: to Rome, to Tehran, …)" | 8 fixed cities: London, New York, Washington, Berlin, Paris, Tokyo, Dubai, Sydney | Any "to X" / "to go to X" / "travelling to X" phrase (Title-Case, or lower-case up to 2 words — "i wanna go to dubai" works); a bare city name typed alone; or a combined sentence like "I want to go to Frankfurt from Berlin" that fills **both** destination and origin in one message |
| Q2 | `origin_city` | "Where would you like to travel from? (e.g: from Rome, from Tehran, …)" | 10 fixed cities: Rome, Madrid, Amsterdam, Vienna, Toronto, Singapore, Istanbul, Cairo, Mumbai, Seoul | Same as Q1, keyed on "from X" / "leaving from X" / "coming from X" / "starting from X"; rejected (with a message) if it matches the destination |
| Q3 | `travel_date_from` | "When are you going to leave? (Select one of the buttons or provide a specific date like 2026/12/16.)" | Tomorrow, This weekend, Next weekend, Next Sunday, Next Monday, Next week, In two weeks | Any recognisable date/day phrase — relative ("today", "tomorrow", "day after tomorrow", "this/next weekend", "next week", "in two weeks", a weekday name) or absolute ("2026-12-16", "16/12/2026", "16th December", "Dec 16", ...); also accepts a combined "from **X** to **Y**" message that fills Q3 **and** Q4 at once |
| Q4 | `travel_date_to` | "And when are you going to come back? (Specify a day or provide a specific date like 2026/12/16.)" | *(none — free text only)* | Same date grammar as Q3, resolved **relative to the departure date** (so "next week"/"the weekend" means the week/weekend after leaving, not after today); rejected if it isn't after the departure date |
| Q5 | `num_travelers` | "How many people are travelling?" | Just me, 2, 3, 4 or more | A whole number 1–20 typed alone; an exact solo phrase from a fixed list in `validate_num_travelers` ("me", "just me", "only me", "myself", "only myself", "solo", "alone", "by myself", "one person", "solo traveller", ...) mapped to 1; or a sentence from which the NLU extracts the number ("a group of 8" and "we are 3 people" worked in the final test). **Not understood:** "7 travellers", "6 of us total", "I'm going alone", "it'll just be me travelling" (see Known limitations) |
| Q6 | `budget_amount` | "What's your total budget for the trip? You can type an amount (e.g. \"430 EUR\") or pick a range:" | Under €350, €350–€850, €850–€1300, €1300–€2100, €2100+ (the buttons send 300, 600, 1075, 1700 and 2500) | A bare number ("800", "4,500"), a number in a sentence ("my budget is 800 EUR"), or a range ("500 to 800", "350-850", "between 350 and 850") reduced to the midpoint of its first two numbers. **The currency is always EUR**: a typed currency ("1,200 USD") is read as a plain number and the currency is ignored (`budget_currency` slot). **Known fault:** "EUR350 - EUR850" is read as a currency-exchange request |
| Q7 | `sustainability_level` | "How much do you want sustainability to drive these recommendations?" | 🌱 Prioritise low carbon, ⚖️ Balance carbon and cost, 💸 Prioritise low cost | The words "low"/"medium"/"high" directly, or a keyword match checked in the order high → low → medium (green/eco/sustainab*/carbon/environment/most → high; cheap/budget/cost/price/money → low; balance/mix/middle/moderate/both → medium). Because "high" is checked first, "keeping costs down matters most" gives **high** (see Known limitations) |
| Q8 | `priority_focus` | *(adaptive — only asked when Q7 = high)* "Since low-carbon travel matters most to you, one more thing: what should I optimise for first when options trade off against each other?" | Lowest carbon whatever it costs, Lowest cost within reason, Balanced | "carbon"/"cost"/"balanced" directly, or a keyword match checked in the order carbon → cost → balanced (carbon/green/emission/eco; cost/cheap/price/money/budget; balance/mix/both/middle). "an even mix of cost and carbon" gives **carbon**, because "carbon" is checked first |

Once Q8 (or Q7, if Q8 is skipped) is answered, `action_generate_trip_recommendations`
runs automatically — no question, no buttons — geocoding both cities and
fetching flights/hotels/transport/attractions/weather concurrently, then
computing the actual **number of nights from the two dates you gave**
(`date_resolver.nights_between`), not a fixed guess. It posts the trip
summary card and immediately activates the booking question below.

**Sentence formula.** One sentence can fill four slots at once, for
example "I want to go to a travel from today to next week from Paris to
Berlin" (dates and cities in one message). The bot then asks only for the
slots that are still missing, starting with "How many people are
travelling?".

### Booking confirmation — `book_confirmation_form` (1 slot)

Shown right after every trip summary, no separate trigger needed.

| Question | Buttons | Free-text alternatives |
|---|---|---|
| "Would you like to book this?" | ✅ Yes, book it (`/confirm_booking`) · No, not this time (`/deny_booking`) | **Yes**-meaning: yes, yeah, yep, sure, ok, good, great, perfect, correct, confirm, "book it", "go ahead", "please do it", "let's book it", ... · **No**-meaning: no, nope, nah, "no thanks", "not now", "not this time", "let me think about it", "maybe later", "cancel", "never mind", "please don't", ... |

- **Yes** → a booking reference (`ECO-XXXXXX`), the same footprint recap
  and attractions blurb as below, plus **🌱 Offset** and **Finish / main
  menu** buttons.
- **No** → the conversation resets straight to the main menu (same
  outcome as pressing "Clear conversation"), with no closing summary. The
  trip is cleared too.
- While this question is open, any other text that is not a clear yes or no
  (for example "what is my carbon footprint?") gets "Sorry, I didn't quite
  catch that — would you like to book it, yes or no?".

### Weather Checking — `weather_check_form` (2 slots)

**Entry:** the "🌤️ Weather Checking" button, or typing e.g. "what's the
weather in Rome" (which can also supply the city, and even the day, in
that same first message).

| # | Slot | Question | Buttons | Free-text alternatives |
|---|---|---|---|---|
| Q1 | `weather_city` | "Which city's weather do you want to check? (Paris, Berlin, New York, Tehran, …)" | *(none)* | Any city name; "in X" / "for X" / "at X" / "of X"; possessive "Berlin's weather"; or a bare "Tehran weather"/"Rome forecast" compound phrase. Same fuzzy-match "Did you mean X?" confirmation as Trip Assistant |
| Q2 | `weather_day_text` | "Is there a specific day you have in mind? (e.g. 2026/12/16)" | Today, Tomorrow, This weekend, Next weekend, Next Sunday, Next week (this day) | Same date grammar as `travel_date_from`; can also be given in the **same** message as the city ("what's Berlin's weather today?", "check the weather of Tehran at the weekend") |

`action_answer_weather_check` then reports the temperature, rainfall and
a one-line piece of advice for that day, followed by **Finish / main
menu**.

### Currency Exchange — `currency_exchange_form` (3 slots)

**Entry:** the "💱 Currency Exchange" button, the home-screen's two
quick-pair shortcuts ("EUR → USD" / "USD → EUR", which pre-fill **both**
currencies and skip straight to the amount), or typing e.g. "convert 100
EUR to USD".

| # | Slot | Question | Buttons | Free-text alternatives |
|---|---|---|---|---|
| Q1 | `from_currency` | "Which currency to which currency?" | 10 fixed pairs: EUR→USD, USD→EUR, EUR→GBP, GBP→EUR, USD→JPY, JPY→USD, USD→GBP, GBP→USD, EUR→CHF, USD→CAD — each button fills **both** currencies at once | A 3-letter ISO code ("EUR") or common name ("euro", "dollar", "pound", "yen", "swiss franc", "yuan", ...); or a combined "EUR to USD" / "EUR->USD" / "EUR/USD" phrase that fills both currencies from one message |
| Q2 | `to_currency` | *(only asked if Q1 didn't already supply both — no button of its own)* | *(none)* | Same code/name grammar as Q1 |
| Q3 | `exchange_amount` | "How much amount? (e.g. 500, 1000, 350, …)" | *(none)* | Any positive number, plain or embedded in a sentence |

`action_answer_currency_exchange` converts using live ECB reference rates
(Frankfurter) and prints `"<amount> <FROM> = <converted> <TO>"`, followed
by **Finish / main menu**.

### Human Advisor — `human_advisor_form` (1 slot)

**Entry:** the "🧑‍💼 Human Advisor" button, or typing e.g. "I'd like to
speak to a specialist".

| Question | Buttons | Free-text alternatives |
|---|---|---|
| "Sure — please share an email or phone number so one of our specialists can reach you as soon as possible. If you already have a reference code from a previous request, please include that as well." | *(none)* | Any text up to 200 characters (no format validation beyond that) |

`action_submit_human_advisor` replies with a reference (`ADV-XXXXXX`),
logs a `proactive_human_advisor` request to the handover queue, and shows
**Finish / main menu**.

### Direct human handover — `request_human_handover` (no form, no button)

Typed at any point, in any flow, e.g. "connect me with an agent", "this
isn't working, get me a human", "I need a real travel agent". Unlike the
Human Advisor flow above, there is no button that sends this intent
directly — it only fires from free text. It immediately runs
`action_handover_to_human`: packages the last 20 turns of transcript,
every collected trip-planning slot, and the last intent/confidence,
prints it to the action server's console for a human advisor to pick up
right away (nothing is written to disk — see "The fallback &
human-handover system" below), and shows a handover card with its own
reference (`HUM-XXXXXX`).

### Hotel list — `hotel_list_form` (1 slot, standalone)

**Entry:** the "🏨 Hotel list" home-screen button (`/see_hotel_list`), or
typing e.g. "show me a hotel list" / "what hotels do you have?".

| Question | Buttons | Free-text alternatives |
|---|---|---|
| "Which city would you like to see hotels for?" | *(none)* | Any city name; "in X" / "for X" / "at X" / "of X" phrasing. Same fuzzy-match confirmation as Trip Assistant |

`action_answer_hotel_list` lists up to 8 hotels for that city (name, star
rating, eco-certification), followed by **Finish / main menu**. This is
independent of Trip Assistant — it doesn't require a trip to already be
planned.

**Drifting into a trip.** Right after the hotel list (or a weather check),
typing e.g. "make a trip to there" / "take me there" starts Trip Assistant
for that same city, even though the Finish prompt is already showing.
Both flows record the city they just answered in the `last_checked_city`
slot, so "there" always means whichever city was looked at most recently
(`action_start_trip_from_weather_drift` fills `destination_city` with it
and the form resumes at Q2). If the message itself names a destination
("... from Berlin to Paris ..."), that city wins over "there". If no city
has been checked yet, Trip Assistant simply starts and asks Q1.

### Cities list — one-shot, no form, no question

**Entry:** the "🗺️ Cities list" home-screen button (`/see_cities_list`),
or typing e.g. "what cities do you have data for?". There's nothing to
answer here — `action_show_cities_list` immediately lists every
destination `data_services` has data for, grouped by continent, followed
by **Finish / main menu**.

### Carbon footprint & offset info — standalone intents, no form, no button trigger of their own

- **`ask_carbon_footprint`** — typed **after a booking has been
  confirmed** (e.g. "what's the carbon footprint of this trip?"). It reuses
  the **last** trip planned via Trip Assistant (`trip_results` slot) to
  break the same route's estimated CO2e down by transport mode (final test,
  Paris to Berlin: coach 24, train 31, petrol car 149, short-haul flight
  219 kg). While the booking question is still open, the text is treated as
  an unclear yes/no (see above). If no trip exists — none planned yet, or
  the trip was cleared by "No, not this time" — the bot says "I don't have
  a trip to compare yet" and asks you to plan one first.
- **`ask_carbon_offset_info`** — typed (e.g. "what is carbon offsetting?"),
  or reached via the **🌱 Offset** button shown after a confirmed booking
  (payload `/ask_carbon_offset_info`). Explains what carbon offsetting is,
  says that an offset does not cancel the flight's emissions, and points to
  Gold Standard and Atmosfair as real, independently audited registries,
  then shows **Finish / main menu**.

### "See more options" — `see_more_options` intent, typed only, no button

Typed **after a booking has been confirmed** (e.g. "show me more
options", "any other hotels?"). Before that, while the booking question is
open, it is treated as an unclear yes/no. There is no button anywhere in
the frontend that sends this — it's reachable purely by typing. Re-lists
all the ranked hotel candidates from the last `trip_results` (5 hotels in
the final test, Paris to Berlin) as a `more_hotels` card.

### Finish / main menu and Clear conversation

- **`/finish`** — the "Finish / main menu" button shown at the end of
  every flow (weather, currency, human advisor, cities/hotel list, offset
  info, and the post-booking trip summary). It ends the process exactly
  like the Clear conversation button: no closing message, the whole
  conversation state is wiped (`Restarted()`, which also clears the
  `awaiting_finish` gate and the fallback streak) and the chat returns to
  the home screen. Typing `finish` / "I'm done" / "wrap up" does the same
  (trained `finish` intent); the reply carries a `reset_to_menu` marker so
  the frontend clears its own chat even though typed text can't be
  recognised before it is sent.
- **`/clear_conversation`** — the "↺ Clear conversation" button, always
  present in the sidebar, plus the one shown automatically after 3
  consecutive off-topic messages (see below). Wipes the whole
  conversation state (`Restarted()`) and starts a fresh session — this
  one has no typed equivalent by design (it's a payload-only intent, the
  same pattern as the city-confirmation "Yes" button).

Both buttons clear the whole chat window. A tester asked to keep the whole
conversation visible instead; that change is not made (see Known
limitations).

## The fallback & human-handover system

Custom-built rather than Rasa's default two-stage fallback (which only has
two stages) to match this project's spec exactly — see
`actions/fallback.py`. This only engages for messages classified as
genuinely **off-topic** (`out_of_scope`) or **low-confidence**
(`nlu_fallback`, raised by `FallbackClassifier`).

**What counts as a strike.** A message counts when the NLU classifies it as
`out_of_scope` or `nlu_fallback` *and* it cannot be read as an answer to the
open question. Checked on 6 October 2026:

- Unreadable text typed for a budget or a date ("abc", "blah blah") **does
  count** as a strike (streak 1, then 2).
- A typo'd city ("brrlin", "pariss", "lodnon") does **not** count: the bot
  asks "Is this what you mean? **Berlin**" with a **Yes** button, with
  unlimited retries.
- An answer that can be read is accepted even when the NLU intent was wrong
  (in the typed-answers test all 8 budget answers were read correctly,
  although five of these phrases are classified as `out_of_scope` in the
  held-out test): the slot's own `validate_<slot>` logic in `actions.py`
  parses the text first and only checks for an off-topic message when
  parsing fails.

1. **1st & 2nd** time it fires → a nudge back to the bot's actual job,
   with quick-reply buttons generated in Python (not hardcoded in the
   UI) — or, if a question was pending, an apology followed immediately
   by that same question again. (Checked: the first two off-topic
   questions show the menu with 4 buttons.)
2. **3rd** consecutive time in the same detour → a graceful decline ("Sorry,
   I didn't understand what you meant."), plus a "Clear conversation"
   button — then the streak resets, so a *later, separate* detour starts
   counting from zero again.
3. That decline is capped at **5 uses across the whole conversation**
   (`fallback_total_giveups`). On the 6th time the bot would have to
   decline again (and only when no question is currently pending), it
   escalates to `action_handover_to_human` instead.

Every quick-pick button described in the flows above (date buttons, city
grids, "Yes" confirmations) is answered via a structured payload like
`/inform_dates{"weather_day_text": "tomorrow"}`. Rasa's own
`RegexMessageHandler` parses that payload's JSON body into the incoming
message's entities before the action server ever runs; every affected
slot's `extract_<slot>` method reads the value straight out of those
entities (see `_structured_payload_value` in `actions.py`), so every
button in this project resolves its own slot correctly and can never be
mistaken for an unrelated fallback trigger.

Human handover packages the full conversation (collected slots, last
20 turns of transcript, last intent/confidence) and — since this project
has no real ticketing system to send it to — prints it to the action
server's console for a human advisor to pick up in real time. Nothing is
ever written to disk: the package can carry the user's contact info and
conversation transcript, and persisting that indefinitely with no
retention or deletion policy would conflict with this project's "no
persistent user data" requirement (GDPR-aligned by design), so the console
— transient by nature — is the only hand-off point. That substitution is
documented here and in the code on purpose: describing an integration you
didn't build is fine for a coursework report; presenting a console log as
a finished integration would not be.

## Frontend design decisions

The dashboard (`streamlit_app.py`) is a deliberately light, high-contrast
"chat product" look rather than a themed travel-document design — plain
white canvas, one accent colour per speaker, and pill-shaped buttons with
no ambiguity about what's clickable:

| Token | Value | Role |
|---|---|---|
| `--eco-white` | `#FFFFFF` | page/app background, header, sidebar, chat input bar |
| `--eco-black` | `#111111` | body text |
| `--eco-muted` | `#667085` | secondary/help text (badge legend, subtitle) |
| `--eco-green` / `--eco-green-deep` | `#7CF7A8` / `#35C878` | bot chat bubbles + button border/hover accent |
| `--eco-blue` / `--eco-blue-deep` | `#20B8C9` / `#1197AA` | user chat bubbles |
| `--eco-border` | `#E7EAEE` | hairline borders (header, sidebar, input bar) |
| Tier green/amber/red | 🟢/🟡/🔴 emoji badges | the one required colour-coding signal for carbon/cost ranking |

Typography is **Inter** throughout (headings and body alike) for density
and legibility in a chat-first layout; there is no separate serif
display font. The persistent sidebar (visible on every screen, not just
the home screen) holds the 4 main-flow nav buttons, the "Clear
conversation / Main menu" button, and a small static explainer box for
the badge legend and ranking methodology. The home screen's welcome
subtitle and the 4-column button grid (Trip Assistant/Hotel list/Cities
list, Weather, Currency + quick pairs, Human Advisor) are shown **only**
while the chat history is empty and no click is already in flight —
they disappear as soon as the first message is sent and never reappear
until "Clear conversation" starts a fresh session. In the chat history,
only the most recent assistant message's buttons stay clickable; every
earlier message's buttons render inert once superseded.

Result cards (`custom` JSON payloads from the backend) are rendered by a
small, explicit registry (`_CARD_RENDERERS` — `trip_summary`,
`more_hotels`, `handover`); anything with an unrecognised `card_type`
falls back to a raw `st.json()` dump on purpose, so a new card type is
never silently swallowed during development.

Accessibility: the green/amber/red badges always come with words as well
as colour. English is the only language, there is no voice input or output,
and no screen-reader test was done.

## Testing

Six independent layers, each catching a different class of regression —
assignment Section 5 ("Testing") asks specifically for the NLU and Core
rows:

| Layer | Tool | What it checks | Train/test overlap? |
|---|---|---|---|
| Custom unit/integration tests | `pytest tests/` | Python logic in `actions/*.py` and `data_services/` — slot extraction, validation, scoring, date math, simulated service outages, the duplicate form-run guard, content-drift guards | N/A (no NLU data involved) |
| **NLU — held-out test set** | `rasa test nlu -u tests/test_nlu.yml` | Intent classification + entity extraction, against **hand-written, never-trained-on** phrasing | **None** — mechanically enforced, see below |
| **NLU — cross-validation** | `rasa test nlu --cross-validation --folds 5 -u data/nlu.yml` | Same, but the assignment's own prescribed method: 5 folds carved out of `data/nlu.yml` itself | **None by construction** — a fold is never in its own training split |
| **Core/dialogue** | `rasa test core --stories tests/test_stories.yml` | Whether the trained policies predict the right next action — story transitions, content drift, task switching, fallback recovery | N/A (no NLU text is parsed — every story step is `intent:`/`entities:`, not raw text) |
| **Story/rule consistency** | `rasa data validate --data data/ --domain domain.yml --config config.yml` | Static check of `data/stories.yml` + `data/rules.yml` + `domain.yml` **together** — catches a contradicting rule/story pair (the exact "InvalidRule" class of bug hit repeatedly earlier in this project) before you even get to training | N/A — static analysis, no model involved |
| **Hand tests on the running bot** | typing into the live bot (or POSTing to `/webhooks/rest/webhook`) | What the bot actually says: typed answers inside forms, typo handling, fallback strikes, Finish reminder, booking, recommendations | N/A |

Note on the validation layer, since it's easy to misread its output: it warns
about intents/utterances "not used in any story or rule" for every
form-slot-filling intent (`affirm`, `deny`, `confirm_booking`,
`inform_num_travelers`, etc.) and every `utter_ask_<slot>` response — this
is a known false-positive for form-based projects (the validator doesn't
know forms absorb these generically via domain.yml's slot mappings, since
it only looks for them as literal steps in stories/rules). The check that
actually matters is the one at the very end of the output —
**"No story structure conflicts found"** (confirmed at both the default
and at `--max-history 8`, matching `config.yml`'s own `TEDPolicy` setting)
— run this after any change to `data/rules.yml`/`data/stories.yml`.

### Running everything

```bash
pip install -r requirements-dev.txt     # once: pytest and httpx

pytest tests/ -q
rasa test nlu -u tests/test_nlu.yml  -m models/<model>.tar.gz --out results/nlu_held_out
rasa test nlu --cross-validation --folds 5 -u data/nlu.yml --out results/nlu_cross_validation
rasa test core --stories tests/test_stories.yml -m models/<model>.tar.gz --out results/core
rasa data validate --data data/ --domain domain.yml --config config.yml
```

Each `rasa test` command writes a confusion matrix, a histogram and a JSON
report into its `--out` folder — that's what "analyse confusion matrices"
in the assignment means in practice. The numbers below come from these
commands, run on 6 October 2026 against the current `data/nlu.yml` and a
model trained from it. The output files are in `results/` (it is **not** in
`.gitignore`); `results/README.md` lists every file.

**Training is random.** DIET starts from random weights, so a re-run on
your own trained model moves the NLU numbers by about ±2 points. Core,
`rasa data validate` and pytest give the same result every time.

### No train/test overlap — enforced, not just promised

`tests/test_nlu.yml` is a from-scratch paraphrase set (different wording,
different cities/currencies/amounts than anything in `data/nlu.yml`) for
**every** trainable intent. `tests/test_nlu_no_overlap.py` loads both files
on every `pytest` run and fails the build if a single held-out example is
an exact (entity-markup-stripped, case/whitespace-normalised) duplicate of
a training example — so this constraint can't silently regress later.
Cross-validation needs no such check: Rasa constructs each fold's
train/test split itself from `data/nlu.yml`, so a fold is never evaluated
on data it was trained on, by definition.

### Results of 6 October 2026

| Test | What was run | Result | Files |
|---|---|---|---|
| NLU, held-out | 268 phrases never used in training (training set: 520 examples; 29 trained intents, 14 entities) | Intent accuracy **75.0%** (201 of 268), weighted F1 0.752, macro F1 0.729. **6 of 29** intents reach F1 0.85; 13 of 29 reach 0.75. Entity micro F1 0.51 | `results/nlu_held_out/` |
| NLU, 5-fold cross-validation | `data/nlu.yml`, each fold 80/20 (28 intents testable; `affirm_city_suggestion` is payload-only) | Test accuracy **71.7%** (±0.040), train accuracy **99.7%** (±0.002), test F1 0.704. Entity F1: test **0.48** (±0.08), train 0.995 | `results/nlu_cross_validation/` |
| Core | 33 test stories (`tests/test_stories.yml` has 34 story blocks; Rasa reports 33 conversations): happy paths, three sustainability levels, task switches, content drift, off-topic input | **33 of 33 pass**, action accuracy 100% (173 actions), no warnings. `TEDPolicy_report.json` is empty: TEDPolicy decided no action, so this tests the rules | `results/core/` |
| `rasa data validate` | stories + rules + domain together | No story structure conflicts. Only the expected "not used in any story or rule" warnings (see note above) | — |
| pytest | 206 tests | **204 pass, 2 fail** (22.8 s). The two failures are `test_city_matching.py::TestStructuredButtonPayloadFullExtractionPipeline::test_destination_city_grid_button_click_resolves_through_full_pipeline` and the matching `..._origin_city_grid_button_...` test. The same two failed in the earlier run (190 of 192) and are not fixed | `results/pytest.txt` |
| Typed answers inside forms | 30 held-out answers typed into the live form at the matching question | Budget **8 of 8** read correctly, sustainability **6 of 7**, priority focus **4 of 6**, travellers **2 of 9** | `results/manual_checks/typed_answers_in_forms.txt` |
| Recommendations | 6 routes × 3 sustainability levels (18 runs, transport options) | At `high`: the lowest-carbon option is first on all 6 routes. At `low`: the cheapest is first on all 6 (Madrid–Lisbon ties at 65). About 1.3 s per answer with two live sources blocked on the test machine (saved data used) | `results/manual_checks/recommendation_sample.txt` |

**What the NLU numbers say.** The two methods agree (75.0% and 71.7%), and
cross-validation shows the gap between 99.7% on training data and 71.7% on
new data: the model fits its training phrasings much better than new ones.
The target of F1 0.85 per intent is **not met**.

- **Weakest intents (held-out F1):** `inform_budget_amount` 0.36 (five of
  eight budget phrases were read as `out_of_scope`), `out_of_scope` 0.41,
  `affirm`, `deny` and `goodbye` 0.60, `inform_sustainability_level` 0.62.
  **Strongest:** `ask_carbon_footprint` and `ask_carbon_offset_info` 1.0.
- **Of the 67 mistakes, 48 have confidence below 0.65**, the
  `FallbackClassifier` threshold in `config.yml`, so the bot goes to its
  fallback and asks again. 19 are confident mistakes, for example "can you
  arrange a trip from Geneva to Florence?" → `ask_trip_to_this_city`
  (0.98), "new plan, Baku instead" → `ask_trip_planning` (0.999), "what's
  AUD worth in CAD?" → `bot_challenge` (0.76).
- **Cross-validation confuses** `inform_priority_focus` with
  `inform_sustainability_level` (7), `ask_trip_planning` with
  `ask_trip_to_this_city` (5), `request_human_handover` with
  `ask_human_advisor` (5) and `out_of_scope` with `bot_challenge` (5). These
  pairs share vocabulary on purpose.
- **Forms rescue most weak intents.** The form's own `validate_<slot>` code
  reads the typed text itself, so a wrong NLU intent often does not matter
  (all 8 budget answers were read correctly, although five of these phrases
  are classified as `out_of_scope`). The exception is the travellers
  question (2 of 9), see Known limitations.
- **The next step is more training examples** for exactly those intents,
  not a pipeline change.

**Earlier NLU runs on the same 268 held-out phrases** (kept in
`results/earlier_runs/`): 73.9% on 1 October (baseline), 73.5%, 74.6% and
76.5% on 3 October after rounds of extra training examples. The run of
6 October (75.0%) falls inside that range; training randomness explains
differences of this size. The earlier cross-validation gave 68.3% test
accuracy. Examples were added between the first and last of these runs, so
the data set is still too small to move the result much.

### Hand tests (6 October 2026)

Done on the running bot with a model trained from the current files.

| Check | Result |
|---|---|
| Greeting, thanks, "are you a bot?", goodbye | Answered |
| Typo "brrlin", "pariss", "lodnon" | "Is this what you mean? **Berlin**" with a Yes button, no strike |
| Budgets "800", "my budget is 800 EUR", "500 to 800", "350-850", "between 350 and 850", "4,500", "I can spend about 600 euros" | 800, 800, 650, 600, 600, 4500, 600 (currency stays EUR) |
| "350 EUR - 850 EUR" at the budget question | Read as 850 |
| "EUR350 - EUR850" at the budget question | **Fault:** starts a currency exchange |
| Unreadable text ("abc", "blah blah") for a budget or date | Counts as a strike (streak 1, 2) |
| Three off-topic questions in a row | 1st and 2nd show the menu with 4 buttons; 3rd says "Sorry, I didn't understand what you meant." with the Clear button |
| New request after a finished weather flow | "Your task completed successfully — please click Finish / main menu below ..." |
| "No, not this time" at the booking question | Chat reset to the menu; a later "what is my carbon footprint?" says there is no trip |
| Carbon footprint and "show me more options" after "Yes, book it" | Work (carbon by mode; 5 hotels) |
| Same two phrases while the booking question is open | Treated as an unclear yes/no |
| Sentence formula "I want to go to a travel from today to next week from Paris to Berlin" | Fills four slots, then asks "How many people are travelling?" |
| "I want to plan a trip" | **Fault:** "Is this what you mean? Plauen" |

On **Paris to Berlin at the low and medium sustainability levels** the
flight (219 kg CO2e) gets the green badge and the train (31 kg) the red
one, because the badge follows the blended rank, not the carbon value.

### A note on how the Core test stories are written

`rasa test core` only evaluates **policy predictions** — it never executes
real action code, so no external API is called and nothing here is
non-deterministic. That also means it has no way to "see" the three
places in this project where a custom action decides the next step via a
runtime `FollowupAction` rather than a trained rule (`action_route_entry_point`,
`action_generate_trip_recommendations` → `book_confirmation_form`,
`action_handle_task_switch`) — this is the same reason `data/stories.yml`'s
own training stories stop right after those actions (see that file's header
comment). The test stories below do the same: a handful of short "entry
routing" stories test only the learnable half of each hand-off, and every
other story tests a form's internal behaviour by starting with
`active_loop: <form>` already set, which the trained rules handle
completely and correctly (confirmed at 100% once structured this way — an
earlier attempt that asserted the dynamic hand-offs directly failed
immediately and consistently, which is what led to this design).

### Why 100% on the Core test is expected here, not a red flag

An ML classifier scoring 100% on a held-out set is usually a sign of
leakage — but `results/core/TEDPolicy_report.json` is literally `{}`
(empty): of the 173 action predictions across the 33 test stories, **zero**
were answered by `TEDPolicy` (the one trainable/statistical component in
the Core pipeline) — every single one came from `RulePolicy`, a
deterministic rule-matcher that replays `data/rules.yml` exactly, not a
model generalising from examples. So this suite is really checking "does
the deployed rule set still match its own specification", the same kind
of thing the `pytest` suite already checks for `actions/*.py` — not
"does a statistical model generalise to unseen input" (that question is
answered, honestly and non-trivially, by the NLU numbers above). A
genuinely stronger Core test would need scenarios no single rule covers
end-to-end (chained, multi-edge-case sequences) so `TEDPolicy` actually has
to arbitrate — a real next step if deeper Core coverage is wanted, not yet
done here.

### What pytest does not cover

24 of the 32 action and validator classes are called directly in pytest.
Eight have no direct test and are covered only by the Core stories (which
check which action runs) and by hand tests: `ActionClearConversation`,
`ActionGetCarbonFootprint`, `ActionHandleDestinationChange`,
`ActionHandleTaskSwitch`, `ActionHandoverToHuman`, `ActionSeeMoreOptions`,
`ActionSmartFallback` and `ValidateHumanAdvisorForm`. The assignment asks
for one test per action (success, failure, edge case); that is only partly
done.

### User testing

Ten people tried the bot and gave feedback: (1) voice input would be
better, (2) the looks matter and give a good feeling, (3) switching between
functions works well, (4) the whole chat should stay visible instead of
being cleared. Voice is future work. Finish and Clear still clear the chat.

## Known limitations

Documented here so they can be cited honestly in the report rather than
discovered by a marker:

- **Carbon figures are always from the local emission-factor table**, never
  Climatiq, regardless of `CLIMATIQ_API_KEY`. `data_services/carbon.py`
  has a `_climatiq_estimate` helper, but `compare_modes()` never calls it
  — confirmed by reading the function, not assumed. Treat the local table
  (cited from UK DESNZ/DEFRA & EEA conversion factors) as the project's
  one and only carbon data source today; wiring Climatiq in for real would
  mean actually branching on the key inside `compare_modes()`.
- **Hotel nightly price is estimated from star rating**, not a real quote
  (OSM carries almost no price data — see `data_services/hotels.py`'s own
  note). Always labelled `price_is_estimated: true` in the data sent to
  the frontend.
- **Stay length is computed from the two dates you give** (departure and
  return, resolved via `date_resolver.nights_between`) rather than a
  fixed guess — a fixed 3-night default is used only as a last-resort
  safety net if, for some reason, a valid night count still can't be
  derived from the resolved dates.
- **No free fare API exists for rail/coach** across Europe, so those two
  travel options carry a **distance-based fare estimate** (a disclosed
  base-fee + per-km rate in `actions/api_clients.py`'s
  `estimate_surface_fare_eur`, in the same spirit as the flights table's
  own formula for uncurated routes) rather than a real quote — this is an
  estimate, not a live price, and is not presented as one.
- **The currency service uses a deprecated endpoint.** The bot calls Frankfurter's `/v1` API, which Frankfurter lists as deprecated in favour of `/v2` but available indefinitely (checked 6 October 2026). If it stops, the saved rates for about 20 currencies answer.
- **Public transport has no timetables.** It shows stop counts from
  OpenStreetMap, or a saved rating for about 20 cities.
- **The budget is display-only.** It is shown next to the trip but does not
  filter or limit the options. The budget currency is always EUR.
- **Booking is a mock confirmation only.** No payment or real reservation
  API is integrated — this is disclosed to the user in the confirmation
  message itself, not just in this file.
- **Tracker store is the default in-memory store** (see `endpoints.yml`).
  Fine for a demo; a persistent store (SQL/Redis) would be the production
  upgrade path.
- **City-name entity recognition** relies on a mix of DIET's trained
  entities and hand-written regex trigger-word extraction
  (`actions/city_extractor.py`) to generalise beyond the training set. An
  unusual or very small town's name may still not be extracted from a
  free-text sentence at all — the fuzzy-match "Did you mean X?" flow
  only helps once *some* candidate string has been extracted; if nothing
  at all is extracted, the form simply re-asks.
- **NLU quality is below the target** (F1 0.85 per intent): see
  [Results of 6 October 2026](#results-of-6-october-2026).
- **No privacy notice, English only, no voice, no screen-reader test.** The
  chat does not tell the user what is sent where (only place names,
  coordinates and currency codes go to the public data APIs).
- **Finish and Clear erase the whole visible chat.** A tester asked to keep
  the conversation on screen.
- **No deployment.** The bot runs locally; the Docker files are unused and
  untested; Hugging Face Docker Spaces need a paid plan.

### Faults found in the final tests (not fixed)

All were reproduced on the running bot on 6 October 2026. None was changed
in the code afterwards.

1. **"I want to plan a trip"** is answered with "Is this what you mean?
   Plauen": the lenient trigger-word extractor in `actions/city_extractor.py`
   takes "plan a" as a city name. Phrases such as "plan a trip", "trip
   planning" and "help me plan a holiday" work.
2. **A typed budget currency is ignored.** The `budget_currency` slot is
   filled only from a `currency_code` entity, and `data/nlu.yml` has no
   training examples for that entity, so the budget currency is always EUR.
3. **"EUR350 - EUR850"** at the budget question is read as
   `ask_currency_exchange` (confidence 0.89) and starts a currency exchange.
   The buttons and the other range formats work.
4. **`validate_num_travelers`** accepts only a bare number, an exact solo
   phrase from its list, or a number the NLU extracted. "7 travellers",
   "6 of us total", "I'm going alone" and "only myself, nobody else" are
   refused with "Please give me a number of travellers between 1 and 20."
   (2 of 9 typed answers worked). Unlike `validate_budget_amount`, it does
   not pull a number out of a sentence.
5. **Keyword order in `validate_sustainability_level` and
   `validate_priority_focus`.** The keywords are checked in a fixed order,
   so "keeping costs down matters most" gives *high* (it contains "most")
   and "an even mix of cost and carbon" gives *carbon*.
6. **Carbon footprint and "see more options" while the booking question is
   open** are treated as an unclear yes/no; they only work after a
   confirmed booking.
7. **Badge and legend.** The badge follows the blended rank (Paris to
   Berlin at low and medium: flight green, train red), and the legend in
   the sidebar says public transport is factored into the ranking, which it
   is not.

## Project structure

```
eco-travel-advisor/
├── config.yml                  # NLU pipeline + dialogue policies
├── domain.yml                  # intents, entities, slots, responses, forms, session_config
├── endpoints.yml
├── data/
│   ├── nlu.yml
│   ├── rules.yml
│   └── stories.yml
├── actions/
│   ├── actions.py              # form validation + trip recommendation orchestrator
│   ├── fallback.py              # smart fallback (3-nudge/5-giveup) + human handover
│   ├── city_extractor.py        # regex trigger-word city extraction (strict + lenient)
│   ├── date_resolver.py         # relative/absolute day & date phrase parsing
│   ├── api_clients.py            # thin REST client to data_services (+ local carbon/fare formulas)
│   └── scoring.py                 # weighted carbon/price ranking function
├── data_services/                # ONE FastAPI app — every external data need
│   ├── main.py                   # mounts all 8 routers, single entry point
│   ├── common.py                 # shared retry/cache/haversine helpers
│   ├── geocode.py / hotels.py / transport.py / attractions.py /
│   │   weather.py / currency.py / carbon.py / flights.py   # one router per domain
│   └── data/                     # curated fallback datasets (cities, attractions, flight routes, ...)
├── tests/
│   ├── test_*.py                 # pytest suite (206 tests)
│   ├── test_nlu.yml              # held-out NLU test set (268 phrases)
│   └── test_stories.yml          # Core test stories
├── results/                      # test output behind the numbers above (see results/README.md)
├── streamlit_app.py             # dashboard frontend
├── Dockerfile                   # single-container build (not used for the submission)
├── docker-compose.yml           # four-container topology (not used for the submission)
├── start.sh                     # boots all four processes in the combined container
├── requirements.txt             # runtime dependencies, every version pinned
├── requirements-dev.txt         # test-only dependencies (pytest, httpx)
├── .env.example                 # the environment variables the code reads (none required)
└── .gitignore                   # models/, .env, .venv/ ... (results/ is kept)
```

## Environment variables

No variable is required. Nothing loads a `.env` file automatically; set a
variable in the shell before starting the process (see `.env.example`).

| Variable | Default | Used by |
|---|---|---|
| `RASA_URL` | `http://localhost:5005` | `streamlit_app.py` |
| `DATA_SERVICES_URL` | `http://localhost:8000` | `actions/api_clients.py` |
| `CLIMATIQ_API_KEY` | *(unset)* | `data_services/carbon.py` — read into a constant but never actually consulted by `compare_modes()`; setting this currently has no effect (see "Data sources" above) |
| `PORT` | `7860` | `start.sh` — the single port the combined container serves (the port Hugging Face Spaces routes to) |

`endpoints.yml`'s `action_endpoint.url` is hardcoded to `http://localhost:5055/webhook`
rather than driven by an env var — Rasa's YAML loader doesn't expand `${VAR}`
shell substitutions, so an env var there would silently never take effect.
`localhost` is correct for plain local dev and the combined container;
`docker-compose.yml`'s `rasa` service rewrites this file with `sed` at
startup for the multi-container case (see that file's comments).
