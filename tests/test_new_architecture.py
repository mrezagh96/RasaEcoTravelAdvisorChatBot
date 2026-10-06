"""
tests/test_new_architecture.py — the 4-process architecture rebuild
=====================================================================
Covers: random city buttons, dynamic date/currency-pair buttons, the
awaiting_finish mechanism, currency pair text extraction (including a
real word-boundary bug found and fixed during this pass), trip-date
validation with real duration calculation, budget range parsing, and
the weather/currency/human-advisor/hotel-list/cities-list flows.

Run with:  pytest tests/ -v
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from actions import api_clients
from actions.actions import (
    ActionAnswerCurrencyExchange,
    ActionAnswerHotelList,
    ActionAnswerWeatherCheck,
    ActionAskDestinationCity,
    ActionAskFromCurrency,
    ActionAskOriginCity,
    ActionAskTravelDateFrom,
    ActionConfirmBooking,
    ActionDenyBooking,
    ActionFinish,
    ActionGenerateTripRecommendations,
    ActionOffsetAndFinish,
    ActionRouteBookConfirmation,
    ActionRouteEntryPoint,
    ActionShowCitiesList,
    ActionSubmitHumanAdvisor,
    ValidateBookConfirmationForm,
    ValidateCurrencyExchangeForm,
    ValidateHotelListForm,
    ValidateTripPlanningForm,
    ValidateWeatherCheckForm,
)
from actions.fallback import dispatch_offtopic_fallback
from data_services.main import app as data_app

_client = TestClient(data_app)


def _bridge_get(path, params, timeout=20.0):
    r = _client.get(path, params=params)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


class FakeDispatcher:
    def __init__(self):
        self.messages = []

    def utter_message(self, **kwargs):
        self.messages.append(kwargs)


class FakeTracker:
    def __init__(self, slots=None, text="", requested_slot=None, sender_id="t1", intent="ask_trip_planning"):
        self._slots = dict(slots or {})
        self._slots["requested_slot"] = requested_slot
        self.latest_message = {"text": text, "intent": {"name": intent}, "entities": []}
        self.events = [{}] * 5
        self.sender_id = sender_id

    def get_slot(self, name):
        return self._slots.get(name)


class TestDynamicButtons:
    def test_destination_gets_8_fixed_city_buttons(self):
        d = FakeDispatcher()
        ActionAskDestinationCity().run(d, FakeTracker(), {})
        titles = [b["title"] for b in d.messages[0]["buttons"]]
        assert titles == ["London", "New York", "Washington", "Berlin", "Paris", "Tokyo", "Dubai", "Sydney"]

    def test_origin_gets_10_fixed_buttons_disjoint_from_destination(self):
        d = FakeDispatcher()
        ActionAskOriginCity().run(d, FakeTracker(slots={"destination_city": "Berlin"}), {})
        titles = [b["title"] for b in d.messages[0]["buttons"]]
        assert len(titles) == 10
        assert "Berlin" not in titles  # the two fixed lists are disjoint by construction

    def test_destination_and_origin_lists_never_overlap(self):
        from actions.actions import _DESTINATION_CITY_OPTIONS, _ORIGIN_CITY_OPTIONS
        assert set(_DESTINATION_CITY_OPTIONS).isdisjoint(set(_ORIGIN_CITY_OPTIONS))

    def test_destination_buttons_need_no_network_call(self):
        # The whole point of switching off live-sampled buttons: this
        # must render correctly even with data_services fully unreachable.
        with patch.object(api_clients, "_get", side_effect=api_clients.ApiError("simulated outage")):
            d = FakeDispatcher()
            ActionAskDestinationCity().run(d, FakeTracker(), {})
        assert len(d.messages[0]["buttons"]) == 8

    def test_date_options_has_7_buttons(self):
        d = FakeDispatcher()
        ActionAskTravelDateFrom().run(d, FakeTracker(), {})
        assert len(d.messages[0]["buttons"]) == 7

    def test_currency_pairs_has_10_buttons_starting_eur_usd(self):
        d = FakeDispatcher()
        ActionAskFromCurrency().run(d, FakeTracker(), {})
        assert len(d.messages[0]["buttons"]) == 10
        assert d.messages[0]["buttons"][0]["payload"] == '/ask_currency_exchange{"from_currency": "EUR", "to_currency": "USD"}'


class TestAwaitingFinish:
    """The awaiting_finish gate lives in ActionRouteEntryPoint (plain
    Python, reading a slot) rather than in rules.yml — see that
    action's own docstring in actions.py for why a pair of static
    condition-scoped rules doesn't work for this (RulePolicy rejects it
    at training time when the two rules predict different FIRST
    actions for the same intent — confirmed against a real
    "rasa train" run, not just theory)."""

    def test_blocked_and_shows_reminder_while_awaiting_finish(self):
        d = FakeDispatcher()
        events = ActionRouteEntryPoint().run(d, FakeTracker(slots={"awaiting_finish": True}, intent="ask_weather"), {})
        assert events == []
        assert d.messages[0]["response"] == "utter_awaiting_finish_reminder"

    def test_routes_to_the_right_form_when_clear(self):
        d = FakeDispatcher()
        events = ActionRouteEntryPoint().run(d, FakeTracker(slots={"awaiting_finish": False}, intent="ask_weather"), {})
        assert len(events) == 1
        assert events[0]["event"] == "followup" and events[0]["name"] == "weather_check_form"

    def test_every_entry_point_intent_has_a_routing_target(self):
        from actions.actions import _ENTRY_POINT_TARGETS
        entry_intents = {
            "ask_trip_planning", "ask_weather", "ask_currency_exchange",
            "ask_human_advisor", "see_hotel_list", "see_cities_list",
        }
        assert set(_ENTRY_POINT_TARGETS.keys()) == entry_intents

    def test_cities_list_is_run_directly_not_via_followup(self):
        # BUGFIX (real user report + confirmed via real execution):
        # action_show_cities_list is a plain, one-shot action, not a
        # form/loop — reaching it via FollowupAction left NO trained
        # Core transition for "what comes after this action", so
        # config.yml's core_fallback_action_name silently fired
        # action_smart_fallback right after the city list AND its
        # Finish-button prompt printed, with no off-topic input from
        # the user at all (see ActionRouteEntryPoint's own docstring).
        # Fixed by calling ActionShowCitiesList().run(...) directly
        # instead of returning FollowupAction("action_show_cities_list")
        # — this asserts that direct-call behaviour stays in place: no
        # "followup" event to that action name should ever be emitted
        # for this specific entry point again.
        with patch.object(api_clients, "_get", side_effect=_bridge_get):
            d = FakeDispatcher()
            events = ActionRouteEntryPoint().run(
                d, FakeTracker(slots={"awaiting_finish": False}, intent="see_cities_list"), {}
            )
        assert not any(e.get("event") == "followup" for e in events)
        assert "Europe" in d.messages[0]["text"]
        assert d.messages[-1]["response"] == "utter_finish_button"

    def test_finish_acts_like_clear_conversation(self):
        # Finish (button or typed) = Clear: tracker wiped (which also
        # resets awaiting_finish), frontend reset marker, no message text.
        d = FakeDispatcher()
        events = ActionFinish().run(d, FakeTracker(slots={"awaiting_finish": True}), {})
        assert len(d.messages) == 1
        assert d.messages[0]["json_message"] == {"card_type": "reset_to_menu"}
        assert "text" not in d.messages[0] or d.messages[0].get("text") is None
        assert {"event": "restart", "timestamp": None} in events


class TestFallbackMenuButtonsActuallyWork:
    """Regression test for a real, reproduced bug: the fallback menu's 4
    topic buttons (/ask_trip_planning, /ask_weather, /ask_currency_exchange,
    /ask_human_advisor — see fallback.py's _topic_buttons) send the exact
    same payloads as the main menu, which ActionRouteEntryPoint refuses to
    act on while awaiting_finish is still True (see TestAwaitingFinish
    above) — printing "please click Finish" instead of actually routing
    anywhere. awaiting_finish is commonly still True right when this
    fallback message shows (every flow sets it on completion, and nothing
    had cleared it yet), so clicking any of its own buttons silently did
    nothing: confirmed via real execution against a live trained model —
    clicking "Check the weather" from this exact menu printed "Your task
    completed successfully" instead of starting weather_check_form.
    Fixed by clearing awaiting_finish at the moment this message (with
    these buttons) is shown — see dispatch_offtopic_fallback."""

    def test_topic_buttons_message_clears_awaiting_finish(self):
        d = FakeDispatcher()
        tracker = FakeTracker(slots={"awaiting_finish": True, "fallback_streak": 0})
        events = dispatch_offtopic_fallback(d, tracker, pending_slot=None)
        assert d.messages[0]["buttons"]  # the 4 topic buttons were shown
        values = {e["name"]: e["value"] for e in events if e.get("event") == "slot"}
        assert values["awaiting_finish"] is False

    def test_clicking_a_topic_button_afterward_now_routes_correctly(self):
        # End-to-end at the Python level: dispatch_offtopic_fallback's
        # awaiting_finish=False event, applied to the tracker, must be
        # enough for a SUBSEQUENT ActionRouteEntryPoint call to route
        # normally instead of re-showing the reminder.
        d1 = FakeDispatcher()
        tracker = FakeTracker(slots={"awaiting_finish": True, "fallback_streak": 0})
        events = dispatch_offtopic_fallback(d1, tracker, pending_slot=None)
        values = {e["name"]: e["value"] for e in events if e.get("event") == "slot"}

        d2 = FakeDispatcher()
        tracker2 = FakeTracker(slots={"awaiting_finish": values["awaiting_finish"]}, intent="ask_weather")
        routed_events = ActionRouteEntryPoint().run(d2, tracker2, {})
        assert len(routed_events) == 1
        assert routed_events[0]["event"] == "followup" and routed_events[0]["name"] == "weather_check_form"
        assert d2.messages == []  # no reminder message this time

    def test_pending_question_branch_does_not_touch_awaiting_finish(self):
        # When a question IS pending (mid-form), this fires the "let's
        # get back to it" apology instead, with no topic buttons — that
        # branch has nothing to do with awaiting_finish and must not
        # touch it.
        d = FakeDispatcher()
        tracker = FakeTracker(slots={"awaiting_finish": True, "fallback_streak": 0})
        events = dispatch_offtopic_fallback(d, tracker, pending_slot="destination_city")
        names = {e["name"] for e in events if e.get("event") == "slot"}
        assert "awaiting_finish" not in names


class TestCurrencyPairExtraction:
    """Includes a regression test for a real bug found during this pass:
    an unanchored [a-z]{3} alternative matched the tail of unrelated
    words ("w-ANT- to con-VERT") instead of an actual currency code."""

    def test_combined_message_extracts_both_codes(self):
        form = ValidateCurrencyExchangeForm()
        tracker = FakeTracker(text="I want to convert EUR to USD")
        result = asyncio.run(form.extract_from_currency(FakeDispatcher(), tracker, {}))
        assert result == {"from_currency": "EUR", "to_currency": "USD"}

    def test_currency_names_not_just_codes(self):
        form = ValidateCurrencyExchangeForm()
        tracker = FakeTracker(text="euro to dollar")
        result = asyncio.run(form.extract_from_currency(FakeDispatcher(), tracker, {}))
        assert result == {"from_currency": "EUR", "to_currency": "USD"}

    def test_unrelated_sentence_does_not_false_positive(self):
        form = ValidateCurrencyExchangeForm()
        tracker = FakeTracker(text="I want to go to Rome")
        result = asyncio.run(form.extract_from_currency(FakeDispatcher(), tracker, {}))
        assert result == {}


class TestTripDateValidation:
    def test_nonsense_date_rejected(self):
        tp = ValidateTripPlanningForm()
        d = FakeDispatcher()
        result = tp.validate_travel_date_from("blah blah nonsense", d, FakeTracker(), {})
        assert result == {"travel_date_from": None}

    def test_valid_date_accepted(self):
        tp = ValidateTripPlanningForm()
        result = tp.validate_travel_date_from("2026-12-10", FakeDispatcher(), FakeTracker(), {})
        assert result == {"travel_date_from": "2026-12-10"}

    def test_return_before_departure_rejected(self):
        tp = ValidateTripPlanningForm()
        d = FakeDispatcher()
        tracker = FakeTracker(slots={"travel_date_from": "2026-12-20"})
        result = tp.validate_travel_date_to("2026-12-10", d, tracker, {})
        assert result == {"travel_date_to": None}

    def test_valid_range_accepted(self):
        tp = ValidateTripPlanningForm()
        tracker = FakeTracker(slots={"travel_date_from": "2026-12-10"})
        result = tp.validate_travel_date_to("2026-12-20", FakeDispatcher(), tracker, {})
        assert result == {"travel_date_to": "2026-12-20"}


class TestRoundTripDateExtraction:
    """Regression tests for a real user report: "from 2026/12/10 to
    2026/12/16" sent in ONE message (the same "from X to Y" habit
    already supported for cities) used to fill neither date — the
    slot only ever looked at the message once IT was the requested_slot,
    i.e. one date per turn. See extract_travel_date_from/_to and
    _extract_date_range_candidates in actions.py."""

    def test_combined_message_fills_both_dates_in_one_turn(self):
        tp = ValidateTripPlanningForm()
        tracker = FakeTracker(
            slots={"destination_city": "Paris", "origin_city": "Berlin"},
            text="from 2026/12/10 to 2026/12/16",
        )
        departure = asyncio.run(tp.extract_travel_date_from(FakeDispatcher(), tracker, {}))
        assert departure == {"travel_date_from": "2026/12/10"}
        return_leg = asyncio.run(tp.extract_travel_date_to(FakeDispatcher(), tracker, {}))
        assert return_leg == {"travel_date_to": "2026/12/16"}

    def test_combined_message_with_relative_phrases(self):
        tp = ValidateTripPlanningForm()
        tracker = FakeTracker(text="from next Monday to next Friday")
        assert asyncio.run(tp.extract_travel_date_from(FakeDispatcher(), tracker, {})) == {
            "travel_date_from": "next Monday"
        }
        assert asyncio.run(tp.extract_travel_date_to(FakeDispatcher(), tracker, {})) == {
            "travel_date_to": "next Friday"
        }

    def test_a_cities_sentence_is_not_mistaken_for_a_date_range(self):
        # "from Berlin to Paris" has the exact same "from X to Y" shape,
        # but neither span is a real date — date_resolver.resolve_date
        # rejects both, so this must extract NOTHING for either date slot.
        tp = ValidateTripPlanningForm()
        tracker = FakeTracker(text="from Berlin to Paris")
        assert asyncio.run(tp.extract_travel_date_from(FakeDispatcher(), tracker, {})) == {}
        assert asyncio.run(tp.extract_travel_date_to(FakeDispatcher(), tracker, {})) == {}

    def test_already_filled_slots_are_never_overwritten(self):
        tp = ValidateTripPlanningForm()
        tracker = FakeTracker(
            slots={"travel_date_from": "2026-12-01", "travel_date_to": "2026-12-05"},
            text="from 2026/12/10 to 2026/12/16",
        )
        assert asyncio.run(tp.extract_travel_date_from(FakeDispatcher(), tracker, {})) == {}
        assert asyncio.run(tp.extract_travel_date_to(FakeDispatcher(), tracker, {})) == {}

    def test_single_date_answer_still_works_when_it_is_the_requested_slot(self):
        tp = ValidateTripPlanningForm()
        tracker = FakeTracker(text="2026-12-10", requested_slot="travel_date_from")
        assert asyncio.run(tp.extract_travel_date_from(FakeDispatcher(), tracker, {})) == {
            "travel_date_from": "2026-12-10"
        }

    def test_second_from_clause_does_not_leak_into_the_return_date(self):
        # BUGFIX (real user report + real execution): "I want to go to a
        # travel from tomorrow to next week from berlin to lisbon" — one
        # message combining a date range AND a city range — used to fill
        # travel_date_to with the literal garbage string "next week from
        # berlin to lisbon" instead of just "next week", because the
        # return-phrase capture was unbounded and swallowed the SECOND,
        # unrelated "from X to Y" clause (the cities) too. A second
        # standalone "from" must now cut the return-phrase capture off.
        tp = ValidateTripPlanningForm()
        tracker = FakeTracker(text="from tomorrow to next week from berlin to lisbon")
        assert asyncio.run(tp.extract_travel_date_from(FakeDispatcher(), tracker, {})) == {
            "travel_date_from": "tomorrow"
        }
        assert asyncio.run(tp.extract_travel_date_to(FakeDispatcher(), tracker, {})) == {
            "travel_date_to": "next week"
        }

    def test_genuine_single_date_range_is_unaffected_by_the_second_from_guard(self):
        # No second "from" exists in either of these, so the return
        # phrase must still expand all the way to the end of the string,
        # exactly as before this fix.
        tp = ValidateTripPlanningForm()
        tracker = FakeTracker(text="from 2026/12/10 to 2026/12/16")
        assert asyncio.run(tp.extract_travel_date_to(FakeDispatcher(), tracker, {})) == {
            "travel_date_to": "2026/12/16"
        }
        tracker2 = FakeTracker(text="from next Monday to next Friday")
        assert asyncio.run(tp.extract_travel_date_to(FakeDispatcher(), tracker2, {})) == {
            "travel_date_to": "next Friday"
        }


class TestBudgetParsing:
    def test_plain_number(self):
        tp = ValidateTripPlanningForm()
        assert tp.validate_budget_amount("800", FakeDispatcher(), FakeTracker(), {}) == {"budget_amount": 800.0}

    def test_number_in_sentence(self):
        tp = ValidateTripPlanningForm()
        assert tp.validate_budget_amount("my budget is 800 EUR", FakeDispatcher(), FakeTracker(), {}) == {"budget_amount": 800.0}

    def test_range_uses_midpoint(self):
        tp = ValidateTripPlanningForm()
        assert tp.validate_budget_amount("500 to 800", FakeDispatcher(), FakeTracker(), {}) == {"budget_amount": 650.0}


class TestFullTripFlowEndToEnd:
    def test_real_duration_and_capped_forecast(self):
        with patch.object(api_clients, "_get", side_effect=_bridge_get):
            tracker = FakeTracker(slots={
                "origin_city": "Berlin", "destination_city": "Rome",
                "travel_date_from": "2026-12-16", "travel_date_to": "2026-12-23",
                "num_travelers": 2, "budget_amount": 800.0, "budget_currency": "EUR",
                "sustainability_level": "high", "priority_focus": "carbon",
            })
            d = FakeDispatcher()
            events = ActionGenerateTripRecommendations().run(d, tracker, {})

        # json_message IS the custom payload (see the note in
        # ActionGenerateTripRecommendations.run()) — no extra "custom"
        # wrapper. This assertion previously read
        # d.messages[1]["json_message"]["custom"]["data"], which matched
        # the OLD buggy double-nested shape; FakeDispatcher just stores
        # utter_message's raw kwargs, so it never caught the real bug
        # (confirmed separately against the actual rasa_sdk
        # CollectingDispatcher, whose "custom" field is exactly
        # json_message, unnested).
        assert d.messages[1]["json_message"]["card_type"] == "trip_summary"
        results = d.messages[1]["json_message"]["data"]
        assert results["nights"] == 7  # real duration, not the old hardcoded 3
        assert len(results["forecast_days"]) == 5  # capped at MAX_FORECAST_DAYS_SHOWN
        assert "Best way there" in d.messages[0]["text"]
        # The one-line weather note was removed from the chat text on
        # purpose; the forecast is still in the summary card's data above.
        assert "Weather during your stay" not in d.messages[0]["text"]
        # "Would you like to book this?" is no longer dispatched directly
        # by this action — it now activates book_confirmation_form via a
        # FollowupAction, and Rasa Core itself fires that form's own
        # utter_ask_book_confirmation right after activation (see
        # domain.yml's book_confirmation slot comment + rules.yml's
        # "Submit book confirmation form" for the real-user-report root
        # cause this fixes: dispatching it directly here left the
        # fallback system with no pending_slot to recognise, so typing
        # "yes"/"no" instead of clicking a button fell into the generic
        # 4-button fallback menu). Only 2 messages are dispatched by this
        # action itself; the 3rd (the actual question) is a SEPARATE
        # action execution this unit test never reaches, since it calls
        # ActionGenerateTripRecommendations directly rather than through
        # real Rasa Core.
        assert len(d.messages) == 2
        assert {"event": "followup", "timestamp": None, "name": "book_confirmation_form"} in events

    def test_booking_confirmation_has_no_demo_disclaimer(self):
        trip_results = {
            "origin": "Berlin", "destination": "Rome",
            "carbon_by_mode": [{"mode": "coach", "carbon_kg": 32}],
            "distance_km": 1180, "attractions": [{"name": "Colosseum"}],
        }
        d = FakeDispatcher()
        ActionConfirmBooking().run(d, FakeTracker(slots={"trip_results": trip_results}), {})
        assert "demo" not in d.messages[0]["text"].lower()
        assert "ECO-" in d.messages[0]["text"]

    def test_booking_confirmation_message_sequence_and_buttons(self):
        # Deliberately ONE merged message now (booking ref + carbon +
        # culture + buttons all together) rather than 3 separate
        # bubbles — see the long comment in ActionConfirmBooking.run().
        # Rasa's real OutputChannel always splits a `custom` payload
        # into its own separate bubble even when sent alongside `text`
        # in the same dispatcher.utter_message() call (confirmed
        # against rasa's own channel.py), which is exactly what
        # produced the unwanted 3rd bubble in production — so the real
        # fix is to never attach a `custom` payload here at all.
        trip_results = {
            "origin": "Berlin", "destination": "Rome",
            "carbon_by_mode": [{"mode": "coach", "carbon_kg": 32}],
            "distance_km": 1180, "attractions": [{"name": "Colosseum"}],
        }
        d = FakeDispatcher()
        events = ActionConfirmBooking().run(d, FakeTracker(slots={"trip_results": trip_results}), {})
        assert len(d.messages) == 1
        assert d.messages[0].get("json_message") is None  # no more custom card / demo disclaimer
        assert "ECO-" in d.messages[0]["text"]
        assert "Estimated footprint" in d.messages[0]["text"]
        assert "Colosseum" in d.messages[0]["text"]
        assert d.messages[0]["buttons"][0]["payload"] == "/ask_carbon_offset_info"
        assert d.messages[0]["buttons"][1]["payload"] == "/finish"
        values = {e["name"]: e["value"] for e in events if e.get("event") == "slot"}
        assert values["awaiting_finish"] is True

    def test_decline_booking_resets_to_menu_like_clear_conversation(self):
        # Real-user request: declining "Would you like to book this?"
        # (however it's phrased — a button click or typed "no need",
        # "not now", ...) must return the bot to the initial menu state
        # exactly as if "Clear conversation" had been pressed, rather
        # than showing a closing carbon/offset summary (the old
        # behaviour this test used to assert). See ActionDenyBooking's
        # own docstring for why this uses a reset_to_menu marker on the
        # reply rather than Restarted() alone: streamlit_app.py watches
        # for that marker on ANY bot reply and wipes its local session
        # state, since (unlike the "Clear conversation" button) this can
        # be reached by arbitrary typed text the frontend can't
        # recognise in advance as an outgoing payload.
        trip_results = {
            "origin": "Berlin", "destination": "Rome",
            "carbon_by_mode": [{"mode": "coach", "carbon_kg": 32}],
            "distance_km": 1180, "attractions": [{"name": "Colosseum"}],
        }
        d = FakeDispatcher()
        events = ActionDenyBooking().run(d, FakeTracker(slots={"trip_results": trip_results}), {})
        assert len(d.messages) == 1
        assert d.messages[0]["json_message"] == {"card_type": "reset_to_menu"}
        assert "text" not in d.messages[0] or d.messages[0].get("text") is None
        assert {"event": "restart", "timestamp": None} in events

    def test_offset_button_shows_info_then_finish(self):
        d = FakeDispatcher()
        ActionOffsetAndFinish().run(d, FakeTracker(), {})
        assert d.messages[0]["response"] == "utter_carbon_offset_info"
        assert d.messages[1]["response"] == "utter_finish_button"


class TestBookConfirmationForm:
    """book_confirmation_form — real-user report: typing "yes"/"no" to
    "Would you like to book this?" instead of clicking a button produced
    the generic 4-button fallback menu, because that question used to be
    answered by a plain intent-routed rule with no active_loop/
    requested_slot for the fallback system to recognise as pending (see
    domain.yml's book_confirmation slot comment for the full root
    cause). These are the unit-level checks; a full real-execution trace
    (retrained model + live action server, covering typed phrasings,
    button payloads, off-topic-during-confirmation, and the genuinely-
    idle regression case) was additionally run and passed before this
    change was delivered."""

    def test_typed_yes_phrasings_resolve_to_yes(self):
        vf = ValidateBookConfirmationForm()
        for phrase in ["yes please", "that's good", "ok", "sure", "please book it", "yea do it", "nice", "confirm"]:
            tracker = FakeTracker(text=phrase, intent="affirm")
            assert vf.validate_book_confirmation(phrase, FakeDispatcher(), tracker, {}) == {"book_confirmation": "yes"}

    def test_typed_no_phrasings_resolve_to_no(self):
        vf = ValidateBookConfirmationForm()
        for phrase in ["no need", "not now", "i need to think again", "nope", "no thank you", "cancel"]:
            tracker = FakeTracker(text=phrase, intent="deny")
            assert vf.validate_book_confirmation(phrase, FakeDispatcher(), tracker, {}) == {"book_confirmation": "no"}

    def test_button_payload_intents_resolve_correctly(self):
        # A button click is parsed as an EXPLICIT intent at confidence
        # 1.0 (see this project's payload-intent-parsing note) — the raw
        # "/confirm_booking"/"/deny_booking" text itself is irrelevant,
        # only the resolved intent matters here.
        vf = ValidateBookConfirmationForm()
        yes_tracker = FakeTracker(text="/confirm_booking", intent="confirm_booking")
        assert vf.validate_book_confirmation("/confirm_booking", FakeDispatcher(), yes_tracker, {}) == {"book_confirmation": "yes"}
        no_tracker = FakeTracker(text="/deny_booking", intent="deny_booking")
        assert vf.validate_book_confirmation("/deny_booking", FakeDispatcher(), no_tracker, {}) == {"book_confirmation": "no"}

    def test_gibberish_gets_plain_in_form_rejection(self):
        # No active offtopic intent (FakeTracker defaults to
        # "ask_trip_planning") -> falls through to the form's own plain
        # "didn't quite catch that" rejection, not an offtopic apology.
        vf = ValidateBookConfirmationForm()
        d = FakeDispatcher()
        result = vf.validate_book_confirmation("banana", d, FakeTracker(text="banana"), {})
        assert result == {"book_confirmation": None}
        assert "yes or no" in d.messages[0]["text"].lower()

    def test_route_yes_calls_confirm_booking_directly(self):
        # Locks in the real-execution-verified fix: this must call
        # ActionConfirmBooking().run() directly (plain Python), NOT
        # return a FollowupAction("action_confirm_booking") — see
        # ActionRouteBookConfirmation's own docstring for why the
        # FollowupAction version silently triggered config.yml's
        # core_fallback_action_name (action_smart_fallback) right after
        # a real trained-model run, since that transition was never a
        # state any rule/story could train on.
        trip_results = {
            "origin": "Berlin", "destination": "Rome",
            "carbon_by_mode": [{"mode": "coach", "carbon_kg": 32}],
            "distance_km": 1180, "attractions": [{"name": "Colosseum"}],
        }
        d = FakeDispatcher()
        tracker = FakeTracker(slots={"trip_results": trip_results, "book_confirmation": "yes"})
        events = ActionRouteBookConfirmation().run(d, tracker, {})
        assert not any(e.get("event") == "followup" for e in events)
        assert "Booking confirmed" in d.messages[0]["text"]

    def test_route_no_calls_deny_booking_directly(self):
        d = FakeDispatcher()
        tracker = FakeTracker(slots={"book_confirmation": "no"})
        events = ActionRouteBookConfirmation().run(d, tracker, {})
        assert not any(e.get("event") == "followup" for e in events)
        assert d.messages[0]["json_message"] == {"card_type": "reset_to_menu"}
        assert any(e.get("event") == "restart" for e in events)


class TestWeatherCombinedExtraction:
    """Regression tests for a real user report: varied natural phrasings
    ("check the weather of Tehran", "what is Berlin's weather today?",
    "what is weather of Tehran at the weekend?") used to leave weather_city
    (and any day mentioned in the SAME message) unfilled, re-asking both
    questions from scratch. See extract_weather_city/extract_weather_day_text
    in ValidateWeatherCheckForm, and city_extractor's leftmost/date-phrase
    -rejection matching."""

    def test_simple_of_trigger_fills_city_only(self):
        form = ValidateWeatherCheckForm()
        # intent="ask_weather" — realistic: this message is what actually
        # activates weather_check_form in the first place, so its own
        # entry intent is what a real tracker would carry here (see
        # _is_foreign_task_switch's own_intents param in actions.py; the
        # default FakeTracker intent, "ask_trip_planning", would
        # otherwise be misread as a foreign task-switch away from this
        # very form).
        tracker = FakeTracker(text="check the weather of Tehran", intent="ask_weather")
        assert asyncio.run(form.extract_weather_city(FakeDispatcher(), tracker, {})) == {
            "weather_city": "Tehran"
        }
        assert asyncio.run(form.extract_weather_day_text(FakeDispatcher(), tracker, {})) == {}

    def test_combined_city_and_relative_day_in_one_message(self):
        # The reported bug: "at the weekend" (appearing AFTER "of Tehran")
        # used to win over "of Tehran" under a fixed trigger-word priority
        # order, extracting "the weekend" as the "city" and leaving
        # weather_city empty. Must now extract BOTH correctly.
        form = ValidateWeatherCheckForm()
        # intent="ask_weather" — see test_simple_of_trigger_fills_city_only's comment above.
        tracker = FakeTracker(text="what is weather of Tehran at the weekend?", intent="ask_weather")
        assert asyncio.run(form.extract_weather_city(FakeDispatcher(), tracker, {})) == {
            "weather_city": "Tehran"
        }
        assert asyncio.run(form.extract_weather_day_text(FakeDispatcher(), tracker, {})) == {
            "weather_day_text": "what is weather of Tehran at the weekend?"
        }

    def test_possessive_phrasing_with_today(self):
        form = ValidateWeatherCheckForm()
        # intent="ask_weather" — see test_simple_of_trigger_fills_city_only's comment above.
        tracker = FakeTracker(text="what is Berlin's weather today?", intent="ask_weather")
        assert asyncio.run(form.extract_weather_city(FakeDispatcher(), tracker, {})) == {
            "weather_city": "Berlin"
        }
        assert asyncio.run(form.extract_weather_day_text(FakeDispatcher(), tracker, {})) == {
            "weather_day_text": "what is Berlin's weather today?"
        }

    def test_already_filled_slots_are_never_overwritten(self):
        form = ValidateWeatherCheckForm()
        tracker = FakeTracker(
            slots={"weather_city": "Rome", "weather_day_text": "2026-12-16"},
            text="of Tehran at the weekend",
        )
        assert asyncio.run(form.extract_weather_city(FakeDispatcher(), tracker, {})) == {}
        assert asyncio.run(form.extract_weather_day_text(FakeDispatcher(), tracker, {})) == {}

    def test_hotel_list_city_has_the_same_uniform_fix(self):
        form = ValidateHotelListForm()
        # intent="see_hotel_list" — realistic: this message is what
        # actually activates hotel_list_form in the first place (see the
        # comment on test_simple_of_trigger_fills_city_only above for the
        # same reasoning, applied to this form's own entry intent).
        tracker = FakeTracker(text="show me hotels in Tehran", intent="see_hotel_list")
        assert asyncio.run(form.extract_hotel_list_city(FakeDispatcher(), tracker, {})) == {
            "hotel_list_city": "Tehran"
        }


class TestOtherFlows:
    def test_weather_check_answers_and_shows_finish(self):
        with patch.object(api_clients, "_get", side_effect=_bridge_get):
            d = FakeDispatcher()
            tracker = FakeTracker(slots={"weather_city": "Rome", "weather_day_text": "2026-12-16"})
            events = ActionAnswerWeatherCheck().run(d, tracker, {})
        assert "Rome" in d.messages[0]["text"]
        assert d.messages[1]["response"] == "utter_finish_button"
        values = {e["name"]: e["value"] for e in events if e.get("event") == "slot"}
        assert values["awaiting_finish"] is True

    def test_currency_exchange_answer(self):
        with patch.object(api_clients, "_get", side_effect=_bridge_get):
            d = FakeDispatcher()
            tracker = FakeTracker(slots={"from_currency": "USD", "to_currency": "EUR", "exchange_amount": 100})
            ActionAnswerCurrencyExchange().run(d, tracker, {})
        assert "100 USD" in d.messages[0]["text"]

    def test_human_advisor_submission_includes_contact(self):
        d = FakeDispatcher()
        tracker = FakeTracker(slots={"contact_info": "test@example.com"})
        ActionSubmitHumanAdvisor().run(d, tracker, {})
        assert "test@example.com" in d.messages[0]["text"]

    def test_hotel_list_sub_button(self):
        with patch.object(api_clients, "_get", side_effect=_bridge_get):
            d = FakeDispatcher()
            tracker = FakeTracker(slots={"hotel_list_city": "Berlin"})
            ActionAnswerHotelList().run(d, tracker, {})
        assert "Berlin" in d.messages[0]["text"]
        assert d.messages[1]["response"] == "utter_finish_button"

    def test_cities_list_covers_all_continents(self):
        with patch.object(api_clients, "_get", side_effect=_bridge_get):
            d = FakeDispatcher()
            ActionShowCitiesList().run(d, FakeTracker(), {})
        assert "Europe" in d.messages[0]["text"]
        assert "Asia" in d.messages[0]["text"]

    def test_cities_list_degrades_gracefully_when_data_services_is_down(self):
        # list_cities() caches its result in-process (see api_clients.py) —
        # reset it first so this genuinely exercises the failure path
        # rather than returning an earlier test's successful cache hit.
        api_clients._cities_list_cache = None
        with patch.object(api_clients, "_get", side_effect=api_clients.ApiError("simulated outage")):
            d = FakeDispatcher()
            events = ActionShowCitiesList().run(d, FakeTracker(), {})
        assert "right now" in d.messages[0]["text"]
        assert d.messages[1]["response"] == "utter_finish_button"
        values = {e["name"]: e["value"] for e in events if e.get("event") == "slot"}
        assert values["awaiting_finish"] is True
        api_clients._cities_list_cache = None  # leave a clean cache for tests after this one


class TestReAskSuppressionDuringCityConfirmation:
    """Regression tests for a real bug found during live testing: when a
    fuzzy-match confirmation ("Is this what you mean?") is pending for a
    city slot, Rasa's normal form behaviour (failed validation re-asks
    the same slot) was printing the original question immediately after
    the confirmation, burying its Yes button. Fixed by converting every
    city-asking response into a custom action that stays silent while
    that slot's confirmation is still pending."""

    def test_weather_city_ask_suppressed_while_its_own_confirmation_pending(self):
        from actions.actions import ActionAskWeatherCity
        d = FakeDispatcher()
        ActionAskWeatherCity().run(d, FakeTracker(slots={"pending_city_field": "weather_city"}), {})
        assert d.messages == []

    def test_weather_city_ask_fires_normally_with_nothing_pending(self):
        from actions.actions import ActionAskWeatherCity
        d = FakeDispatcher()
        ActionAskWeatherCity().run(d, FakeTracker(), {})
        assert len(d.messages) == 1

    def test_weather_city_ask_not_confused_by_a_different_pending_slot(self):
        from actions.actions import ActionAskWeatherCity
        d = FakeDispatcher()
        ActionAskWeatherCity().run(d, FakeTracker(slots={"pending_city_field": "destination_city"}), {})
        assert len(d.messages) == 1

    def test_destination_and_origin_and_hotel_list_ask_all_suppress_correctly(self):
        from actions.actions import ActionAskDestinationCity, ActionAskOriginCity, ActionAskHotelListCity
        for cls, slot in [
            (ActionAskDestinationCity, "destination_city"),
            (ActionAskOriginCity, "origin_city"),
            (ActionAskHotelListCity, "hotel_list_city"),
        ]:
            d = FakeDispatcher()
            cls().run(d, FakeTracker(slots={"pending_city_field": slot}), {})
            assert d.messages == [], f"{cls.__name__} should stay silent while its own confirmation is pending"


class TestDateButtonEntityExtraction:
    """Regression tests for a real bug: travel_date_from and
    weather_day_text only had a from_text slot mapping, so a button
    payload like '/inform_dates{"weather_day_text": "tomorrow"}' never
    got its entity extracted — from_text just grabbed the whole raw
    payload string verbatim instead. Fixed by adding from_entity
    mappings (see domain.yml) — this test covers the date_resolver-side
    defence: even a raw, un-extracted payload string must fail cleanly
    rather than accidentally matching a weekday name that happens to
    appear inside its own JSON-ish noise."""

    def test_raw_unextracted_payload_strings_resolve_to_none(self):
        from actions.date_resolver import resolve_date
        for bad in [
            '/inform_dates{"weather_day_text": "today"}',
            '/inform_dates{"weather_day_text": "tomorrow"}',
            '/inform_dates{"weather_day_text": "next Sunday"}',
        ]:
            assert resolve_date(bad) is None

    def test_genuine_date_phrases_still_resolve(self):
        from actions.date_resolver import resolve_date
        for good in ["today", "tomorrow", "this weekend", "next Sunday", "2026-12-16"]:
            assert resolve_date(good) is not None


class TestYesClickThroughFullValidationPipeline:
    """A much stronger version of the direct validate_<slot> tests
    above: this goes through the REAL rasa_sdk ValidationAction.run()
    machinery (get_extraction_events -> get_validation_events), the
    same path actually used in production, catching anything a direct
    function call could miss."""

    @staticmethod
    def _slotset(name, value):
        return {"event": "slot", "name": name, "value": value, "timestamp": None}

    class _PipelineTracker:
        def __init__(self, slots, events, text, sender_id="pipeline-test"):
            self.slots = dict(slots)
            self.latest_message = {"text": text, "intent": {"name": "affirm_city_suggestion"}, "entities": []}
            self.events = list(events)
            self.sender_id = sender_id

        def get_slot(self, name):
            return self.slots.get(name)

        def add_slots(self, slot_events):
            for event in slot_events:
                if event.get("event") != "slot":
                    continue
                self.slots[event["name"]] = event["value"]
                self.events.append(event)

        def slots_to_validate(self):
            out = {}
            for event in reversed(self.events):
                if event.get("event") != "slot":
                    break
                out[event["name"]] = event["value"]
            return out

        def form_slots_to_validate(self):
            return self.slots_to_validate()

    def test_weather_city_yes_click_resolves_through_the_real_pipeline(self):
        from actions.actions import ValidateWeatherCheckForm

        domain = {"forms": {"weather_check_form": {"required_slots": ["weather_city", "weather_day_text"]}}}
        base_events = [{"event": "action", "name": "action_listen"}, self._slotset("weather_city", "/affirm_city_suggestion")]
        tracker = self._PipelineTracker(
            slots={"pending_city_field": "weather_city", "pending_city_suggestion": "Paris", "weather_city": None},
            events=base_events, text="/affirm_city_suggestion", sender_id="pipeline-test-single",
        )
        dispatcher = FakeDispatcher()
        events = asyncio.run(ValidateWeatherCheckForm().run(dispatcher, tracker, domain))
        values = {e["name"]: e["value"] for e in events if e.get("event") == "slot"}
        assert values.get("weather_city") == "Paris"
        assert values.get("pending_city_field") is None
        assert values.get("pending_city_suggestion") is None
        assert dispatcher.messages == []

    def test_a_genuine_duplicate_invocation_is_suppressed_not_reprocessed(self):
        # This is deliberately the SAME scenario the dedup guard exists
        # for: identical (sender_id, event count, message text) is
        # exactly what a real Rasa double-invocation looks like from the
        # action server's side (see _is_duplicate_form_run). The FIRST
        # call must resolve correctly; the SECOND, indistinguishable
        # call must be suppressed (empty events) rather than re-run
        # validation on the same raw payload a second time — which is
        # what used to print "Is this what you mean?" twice, or fall
        # through to "couldn't find a place called '/affirm_city_suggestion'".
        from actions.actions import ValidateWeatherCheckForm

        domain = {"forms": {"weather_check_form": {"required_slots": ["weather_city", "weather_day_text"]}}}
        base_events = [{"event": "action", "name": "action_listen"}, self._slotset("weather_city", "/affirm_city_suggestion")]
        slots = {"pending_city_field": "weather_city", "pending_city_suggestion": "Paris", "weather_city": None}

        tracker_a = self._PipelineTracker(slots=dict(slots), events=list(base_events), text="/affirm_city_suggestion", sender_id="pipeline-test-dup")
        tracker_b = self._PipelineTracker(slots=dict(slots), events=list(base_events), text="/affirm_city_suggestion", sender_id="pipeline-test-dup")
        events_a = asyncio.run(ValidateWeatherCheckForm().run(FakeDispatcher(), tracker_a, domain))
        events_b = asyncio.run(ValidateWeatherCheckForm().run(FakeDispatcher(), tracker_b, domain))

        values_a = {e["name"]: e["value"] for e in events_a if e.get("event") == "slot"}
        assert values_a.get("weather_city") == "Paris"
        assert events_b == []  # the duplicate is a clean no-op, not a second (wrong) resolution attempt


class TestTypedAffirmationForCityConfirmation:
    """Regression test for a real UX gap: only the "Yes" button's exact
    payload was recognised as confirming a fuzzy-matched city — if the
    user typed "yes" (or "yeah", "correct", "ok", ...) instead of
    clicking, it fell through to normal city matching and failed with
    "I couldn't find a place called 'yes'". Only the FIRST word is
    checked, so a real city name is never mistaken for an affirmation."""

    def test_bare_yes_resolves_the_pending_suggestion(self):
        from actions.actions import _looks_like_affirmation
        assert _looks_like_affirmation("yes") is True
        assert _looks_like_affirmation("Yes, Paris") is True
        assert _looks_like_affirmation("yeah that's right") is True
        assert _looks_like_affirmation("ok") is True

    def test_a_real_city_name_is_never_mistaken_for_affirmation(self):
        from actions.actions import _looks_like_affirmation
        assert _looks_like_affirmation("Paris") is False
        assert _looks_like_affirmation("Berlin") is False
        assert _looks_like_affirmation("no") is False

    def test_typed_yes_end_to_end_through_validate_weather_city(self):
        from actions.actions import ValidateWeatherCheckForm
        form = ValidateWeatherCheckForm()
        dispatcher = FakeDispatcher()
        tracker = FakeTracker(slots={"pending_city_field": "weather_city", "pending_city_suggestion": "Paris"})
        result = form.validate_weather_city("yes", dispatcher, tracker, {})
        assert result == {"weather_city": "Paris", "pending_city_field": None, "pending_city_suggestion": None}
        assert dispatcher.messages == []

    def test_typing_a_different_city_still_works_as_a_correction(self):
        from actions.actions import ValidateWeatherCheckForm
        with patch.object(api_clients, "_get", side_effect=_bridge_get):
            form = ValidateWeatherCheckForm()
            dispatcher = FakeDispatcher()
            tracker = FakeTracker(slots={"pending_city_field": "weather_city", "pending_city_suggestion": "Paris"})
            result = form.validate_weather_city("Berlin", dispatcher, tracker, {})
        assert result == {"weather_city": "Berlin"}