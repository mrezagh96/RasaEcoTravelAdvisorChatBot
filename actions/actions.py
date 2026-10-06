"""
actions.py — MGH Travel Chatbot
=====================================================================
The Rasa-facing layer: everything here is an Action or a
FormValidationAction. All the actual API work lives in api_clients.py,
the ranking maths lives in scoring.py, date parsing lives in
date_resolver.py, and the fallback/handover system lives in
fallback.py — this file wires user intent to those building blocks.

Four independent conversational processes, each backed by one Rasa
form (see domain.yml): trip_planning_form, weather_check_form,
currency_exchange_form, human_advisor_form — plus a small
hotel_list_form behind the Trip Assistant's "hotel list" sub-button.
"""

from __future__ import annotations

import concurrent.futures
import random
import re
import string
import time
from abc import ABC, abstractmethod
from datetime import date as date_cls, timedelta
from typing import Any, Dict, FrozenSet, List, Optional, Text

from rasa_sdk import Action, FormValidationAction, Tracker
from rasa_sdk.events import ActiveLoop, EventType, FollowupAction, Restarted, SlotSet
from rasa_sdk.executor import CollectingDispatcher

from . import api_clients, city_extractor, date_resolver, scoring
from .fallback import dispatch_offtopic_fallback, is_offtopic_intent, reset_fallback_events, send_to_human_queue

MAX_FORECAST_DAYS_SHOWN = 5  # cap how many per-day weather lines a long stay prints

# ---- workaround for a known, documented Rasa quirk --------------------------
# RasaHQ/rasa#11595, rasa-sdk#100, and several Rasa community forum threads
# all report the same behaviour: when a form's validate action REJECTS a
# slot (returns None) and re-requests it, Core can invoke the validate
# action TWICE for what is really one incoming user message — duplicating
# every dispatcher.utter_message() sent during validation, and making the
# bot appear to jump straight to the next question without waiting for the
# user. The fix used by other people who hit this (confirmed on the GitHub
# issue thread) is to override the form's run() method with a dedup guard.
# Keyed on (conversation, how many tracker events exist so far, message
# text) — that combination is identical across the duplicate invocation
# within one turn, but different for any genuinely new message. Every
# form-validation class in this file shares this ONE guard via the
# DedupFormValidation base class below, since the underlying Core quirk
# isn't specific to any one form.
_recent_form_runs: Dict[tuple, float] = {}
_FORM_RUN_DEDUP_WINDOW_SECONDS = 2.0


def _is_duplicate_form_run(tracker: Tracker) -> bool:
    text = (tracker.latest_message or {}).get("text", "")
    key = (tracker.sender_id, len(tracker.events), text)
    now = time.time()
    last = _recent_form_runs.get(key)
    _recent_form_runs[key] = now
    if len(_recent_form_runs) > 500:  # opportunistic cleanup, not a real cache
        cutoff = now - 60
        for k, t in list(_recent_form_runs.items()):
            if t < cutoff:
                _recent_form_runs.pop(k, None)
    return last is not None and (now - last) < _FORM_RUN_DEDUP_WINDOW_SECONDS


class DedupFormValidation(FormValidationAction, ABC):
    """Every form-validation class below inherits this instead of
    FormValidationAction directly — see _is_duplicate_form_run's
    docstring for why.

    Genuinely ABSTRACT (via `abc.ABC` + `@abstractmethod` on `name()`
    below), not just "doesn't happen to override name()" — rasa-sdk's
    ActionExecutor._register_all_actions() walks EVERY subclass of
    Action/FormValidationAction it can find and tries to register each
    one, skipping only ones where `inspect.isabstract(...)` is True.
    FormValidationAction's own `name()` raises a plain
    NotImplementedError rather than being a real `@abstractmethod`, so
    without this, `inspect.isabstract()` doesn't recognise this class
    as abstract, and the action server crashes on startup trying to
    instantiate THIS class directly and call its (unimplemented)
    name() — confirmed against a real `rasa run actions` crash with
    exactly that traceback, not a hypothetical.
    """

    @abstractmethod
    def name(self) -> Text:
        ...

    async def run(
        self, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict[Text, Any]
    ) -> List[EventType]:
        if _is_duplicate_form_run(tracker):
            return []
        return await super().run(dispatcher, tracker, domain)


def _safe_call(func, *args, **kwargs):
    """Runs a data-fetching call and swallows ANY exception into a plain
    None, instead of raising. This is what makes it safe to fire several
    of these off at once in a ThreadPoolExecutor: one slow or failing
    API can never take the others down."""
    try:
        return func(*args, **kwargs)
    except Exception:
        return None


# =========================================================================
# Shared city fuzzy-match + confirmation flow — used by trip planning,
# weather checking, and the hotel-list sub-button. A module-level
# function (not tied to any one form class) since three different forms
# need it.
# =========================================================================

# Words that mean "yes, that's the city" when typed instead of clicking
# the "Yes" button — only the FIRST word is checked (a real city name
# essentially never starts with one of these), so "yes that's right" or
# "yeah, Paris please" are recognised the same as a bare "yes".
#
# BROADENED (real user feedback: "this is a chatbot, not an app — it
# should accept things like yes, yea, that's it, exactly, ok too").
# First-word matching alone can't catch a reply like "that's it" or
# "exactly right" (the affirmative word isn't the FIRST one, or the
# whole phrase IS the affirmation), so _AFFIRMATIVE_PHRASES below is
# checked against the ENTIRE normalised message first.
_AFFIRMATIVE_WORDS = {
    "yes", "yeah", "yea", "yep", "yup", "sure", "correct", "right",
    "ok", "okay", "affirmative", "exactly", "alright", "aye",
}
_AFFIRMATIVE_PHRASES = {
    "that's it", "thats it", "that is it",
    "that's right", "thats right",
    "that's correct", "thats correct",
    "that's the one", "thats the one",
    "exactly right", "sounds right", "sounds good",
    "yes exactly", "that works",
}


def _looks_like_affirmation(text: str) -> bool:
    normalised = text.strip().lower().rstrip(",.!?;:")
    if normalised in _AFFIRMATIVE_PHRASES:
        return True
    words = normalised.split()
    if not words:
        return False
    first_word = words[0].rstrip(",.!?;:")
    return first_word in _AFFIRMATIVE_WORDS


# ---- "Would you like to book this?" yes/no phrase matching (real user
# report — see domain.yml's book_confirmation slot comment for the full
# root cause) — deliberately a SEPARATE set from _AFFIRMATIVE_WORDS/
# _AFFIRMATIVE_PHRASES above rather than folding into them: those are
# about confirming a city SPELLING suggestion, this is about confirming
# a BOOKING decision, and the user's own example phrasing here includes
# words ("good", "nice", "please") that don't obviously belong in a
# generic city-suggestion affirmation set. Kept as first-word / whole-
# phrase checks, exactly like _looks_like_affirmation, EXCEPT "please"
# on its own: a bare first-word check would also match "please don't
# book it" / "please cancel" as a YES, which is backwards — so "please"
# is only recognised as a handful of exact, curated phrases instead.
_BOOKING_YES_WORDS = {
    "yes", "yeah", "yea", "yep", "yup", "sure", "ok", "okay", "good",
    "nice", "great", "perfect", "correct", "right", "alright", "confirm",
}
_BOOKING_YES_PHRASES = {
    "that's good", "thats good", "it's ok", "its ok", "it's good", "its good",
    "yes please", "yes it's ok", "yes its ok", "sounds good", "go ahead",
    "book it", "yea do it", "yeah do it", "do it", "let's book it", "lets book it",
    "yes book it", "please", "please do", "please do it", "please book it",
    "yes do it",
}

_BOOKING_NO_WORDS = {"no", "nope", "nah", "not"}
_BOOKING_NO_PHRASES = {
    "no need", "no thanks", "no thank you", "not now", "not this time",
    "no not now", "i need to think again", "i need to think about it",
    "let me think", "let me think about it", "i'll think about it",
    "ill think about it", "maybe later", "not yet", "cancel", "never mind",
    "nevermind", "please don't", "please dont", "please no", "please cancel",
    "please wait",
}


def _looks_like_booking_yes(text: str) -> bool:
    normalised = text.strip().lower().rstrip(",.!?;:")
    if normalised in _BOOKING_YES_PHRASES:
        return True
    words = normalised.split()
    if not words:
        return False
    return words[0].rstrip(",.!?;:") in _BOOKING_YES_WORDS


def _looks_like_booking_no(text: str) -> bool:
    normalised = text.strip().lower().rstrip(",.!?;:")
    if normalised in _BOOKING_NO_PHRASES:
        return True
    words = normalised.split()
    if not words:
        return False
    return words[0].rstrip(",.!?;:") in _BOOKING_NO_WORDS


def _offtopic_fallback_check(
    slot_name: Text, dispatcher: CollectingDispatcher, tracker: Tracker
) -> Optional[Dict[Text, Any]]:
    """Returns a validate_<slot>-style dict (slot_name -> None, plus
    whatever fallback bookkeeping slots need updating) when the CURRENT
    message is genuinely off-topic (see fallback.is_offtopic_intent),
    else None so the caller falls through to its own normal logic.

    Every validate_<slot> in this file calls this FIRST and returns
    immediately when it isn't None — see fallback.py's
    dispatch_offtopic_fallback docstring for why this belt-and-suspenders
    check lives here too, not only in domain.yml's `not_intent`
    restriction: real execution testing found that a handful of slots
    (the ones routed through _match_city_or_ask below, plus
    from_currency) have their own custom extract_<slot> methods that
    bypass that restriction entirely, so relying on domain.yml alone
    left them unfixed."""
    if not is_offtopic_intent(tracker):
        return None
    result: Dict[Text, Any] = {slot_name: None}
    for event in dispatch_offtopic_fallback(dispatcher, tracker, pending_slot=slot_name):
        if event.get("event") == "slot":
            result[event["name"]] = event["value"]
    return result


def _is_content_drift_trigger(tracker: Tracker, *intent_names: Text) -> bool:
    """True exactly when the CURRENT message was classified as one of the
    given content-drifting intents (change_destination_city,
    ask_trip_to_this_city).

    ROOT CAUSE this mirrors (confirmed via a real trained-model run with
    DEBUG-level policy logs, not guessed): domain.yml's `not_intent`
    restriction on out_of_scope/nlu_fallback only gates the DECLARATIVE
    from_entity/from_text mapping layer. RulePolicy only re-predicts and
    lets a condition-scoped "Handle ... during <form>" rule win when the
    form's OWN execution comes back "rejected" — logged as "Execution of
    '<form>' was rejected. Setting its confidence to 0.0 in all
    predictions." — and that only happens when the currently requested
    slot ends up with ZERO extraction candidate this turn. A `type:
    custom` slot (weather_day_text, travel_date_from/_to, origin_city,
    book_confirmation, ...) bypasses `not_intent` entirely — its
    extract_<slot> method still hands back the raw message text as a
    "candidate" regardless of intent, validation then fails it as a bad
    date/number/city, and the form quietly re-asks instead of ever
    rejecting — so the content-drift rules in rules.yml never got a
    chance to fire. Confirmed via real execution: "actually, take me to
    Vienna instead" while num_travelers was requested got treated as a
    failed traveller-count answer; "I want to go there from Berlin"
    while weather_day_text was requested got treated as a failed date.
    The two forms below aren't the same combination for these intents
    (domain.yml's not_intent list handles the from_text/from_entity
    slots; every custom-type extractor calls this helper directly) —
    together they make every one of this form's slots produce the same
    "genuinely no candidate this turn" signal that out_of_scope/
    nlu_fallback already got, so the form rejects and RulePolicy's
    re-prediction can find the matching content-drift rule, exactly the
    same mechanism as is_offtopic_intent above."""
    intent = (tracker.latest_message or {}).get("intent", {}) or {}
    return intent.get("name") in intent_names


def _match_city_or_ask(
    slot_name: Text, candidate: Text, dispatcher: CollectingDispatcher, tracker: Tracker
) -> Dict[Text, Any]:
    """Looks the candidate up via data_services' fuzzy matcher. Returns a
    dict of slot updates:
      - {slot_name: <value>} — exact match, accepted immediately.
      - {slot_name: None} — nothing matched at all, form re-asks.
      - {slot_name: None, "pending_city_field": slot_name,
         "pending_city_suggestion": <name>} — a fuzzy match needs
        confirming first (the "Is this what you mean?" message, with a
        "Yes" button).

    Off-topic check ORDER (real user report — "a city typo can't be
    unrelated data, tone it down"): api_clients.match_city() is tried
    FIRST now, before any off-topic check — a message that fuzzy-matches
    a REAL city is accepted immediately no matter how DIET classified
    it. is_offtopic_intent is only consulted once match_city has
    already failed to find anything, to decide whether THIS particular
    failure should count as an escalating fallback strike
    (out_of_scope, or nlu_fallback on something that plainly isn't a
    city either) or just the plain, unlimited-retries "couldn't find a
    place called '...'" rejection (any other case — most often a typo
    DIET couldn't confidently classify at all, which is exactly the
    "nlu_fallback but still a real answer" case this reordering is
    for). Previously the off-topic check ran BEFORE match_city, so a
    genuine city name that happened to get classified nlu_fallback
    (DIET is least confident on exactly the out-of-vocabulary, rare, or
    typo'd names this fuzzy-matcher exists to rescue) was being treated
    as an off-topic strike before match_city ever got a chance to
    recognise it.

    The "Yes" click is handled RIGHT HERE, at the very top, rather than
    via a separate rule + action (an earlier version tried that, via
    `/affirm_city_suggestion` triggering a dedicated action through a
    rule) — confirmed via a real, reproducible bug that the earlier
    design doesn't reliably fire: this slot's own `from_text` mapping
    (scoped to active_loop + requested_slot, not to any particular
    intent) grabs the RAW "/affirm_city_suggestion" payload text
    automatically as part of Core's normal message processing,
    regardless of what any rule predicts should happen next — so
    `candidate` here can legitimately BE that literal payload string.
    Recognising it directly, unconditionally, is the only place in the
    codebase guaranteed to run on every turn of this slot's validation,
    which is why it's handled here instead of relying on rule priority.
    """
    if tracker.get_slot("pending_city_field") == slot_name and (
        candidate.strip() == "/affirm_city_suggestion" or _looks_like_affirmation(candidate)
    ):
        suggestion = tracker.get_slot("pending_city_suggestion")
        if suggestion:
            return {slot_name: suggestion, "pending_city_field": None, "pending_city_suggestion": None}
        # Affirmed with nothing actually pending (e.g. double-clicked) —
        # fall through to the normal "couldn't find" path below rather
        # than silently doing nothing, so the user isn't left stuck.

    try:
        match = api_clients.match_city(candidate)
    except api_clients.ApiError:
        match = None

    if not match or not match.get("found"):
        # Only NOW check whether this looks like a genuinely off-topic
        # message rather than an unrecognised/typo'd city name — see
        # the "Off-topic check ORDER" note in this function's docstring
        # for why match_city gets first crack at the raw text.
        offtopic = _offtopic_fallback_check(slot_name, dispatcher, tracker)
        if offtopic is not None:
            return offtopic

        # Never echo the raw candidate back if it looks like an
        # unprocessed structured payload (starts with "/") — a user
        # should never see something like "/affirm_city_suggestion" in
        # a message meant for them, even as a safety net for a case
        # that shouldn't reach here any more after the fix above.
        if candidate.strip().startswith("/"):
            dispatcher.utter_message(text="Sorry, I didn't quite catch that — could you type the city name?")
        else:
            dispatcher.utter_message(
                text=f"I couldn't find a place called '{candidate}'. Could you try a nearby larger city or check the spelling?"
            )
        return {slot_name: None}

    if match["is_exact"]:
        return {slot_name: match.get("matched_name") or match.get("display_name") or candidate}

    suggestion = match["matched_name"]
    dispatcher.utter_message(
        text=f"Is this what you mean? **{suggestion}**\nIf not, please write the correct answer…",
        buttons=[{"title": "Yes", "payload": "/affirm_city_suggestion"}],
    )
    return {slot_name: None, "pending_city_field": slot_name, "pending_city_suggestion": suggestion}


# =========================================================================
# Entry-point routing — every flow-starting intent funnels through this
# ONE action first, so the awaiting_finish check lives in exactly one
# place (plain Python, reading a slot — see the "Route ... entry point"
# rules in rules.yml for why this isn't a rule `condition:` block instead).
# =========================================================================

_ENTRY_POINT_TARGETS = {
    "ask_trip_planning": "trip_planning_form",
    "ask_weather": "weather_check_form",
    "ask_currency_exchange": "currency_exchange_form",
    "ask_human_advisor": "human_advisor_form",
    "see_hotel_list": "hotel_list_form",
    "see_cities_list": "action_show_cities_list",
}


class ActionRouteEntryPoint(Action):
    """Every flow-starting intent funnels through this ONE action first,
    so the awaiting_finish check lives in exactly one place (plain
    Python, reading a slot).

    This is DELIBERATELY not expressed as a pair of static rules
    (one "happy path" rule activating the flow directly, one
    condition-scoped "awaiting_finish" rule overriding it) — that
    was tried, and training fails with "InvalidRule: Contradicting
    rules or stories found", because the two rules predict genuinely
    DIFFERENT first actions for the same intent (not just different
    LATER steps, which is what Rasa's condition-scoped-rule support is
    documented to handle cleanly — see the "off-topic during a form"
    rules below, which share the same first action across variants and
    have never had this problem). RulePolicy's `slot_was_set` condition
    mechanism specifically has a long, well-documented history of
    surprising/buggy interactions with initial slot values across Rasa
    versions (confirmed via multiple RasaHQ/rasa GitHub issues), so a
    single dynamic-routing action is the more robust choice here, even
    though it means the entry-point activation itself can't be shown as
    a multi-step sequence in stories.yml (see that file's own note).
    FollowupAction from a plain, non-form-scoped custom action like this
    one is a normal, supported pattern — the "FollowupAction disallowed"
    restriction reported on Rasa's forum is specific to calling it from
    INSIDE a form's own validation logic trying to escape that same
    form's loop, which is a different situation from this one.
    """

    def name(self) -> Text:
        return "action_route_entry_point"

    def run(
        self, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict[Text, Any]
    ) -> List[EventType]:
        if tracker.get_slot("awaiting_finish"):
            dispatcher.utter_message(response="utter_awaiting_finish_reminder")
            return []
        intent = (tracker.latest_message or {}).get("intent", {}).get("name")
        target = _ENTRY_POINT_TARGETS.get(intent)
        if not target:
            return []
        if target == "action_show_cities_list":
            # BUGFIX (real user report + confirmed via real execution):
            # every OTHER target here is a FORM, which is safe to reach
            # via FollowupAction because a form's own active_loop rule
            # ("Submit ... form" in rules.yml) matches regardless of
            # HOW that loop became active — Rasa always knows a form
            # should return to action_listen once it's done asking.
            # action_show_cities_list is the one exception: a plain,
            # one-shot action, not a loop. Reaching it via
            # FollowupAction left NO trained transition telling Rasa
            # Core what to do after it finished (that exact state —
            # previous_action=action_show_cities_list — was never a
            # literal step in any rule/story), so config.yml's
            # core_fallback_action_name silently fired
            # action_smart_fallback right after the city list AND its
            # Finish-button prompt printed, showing the generic
            # 4-button menu with no off-topic input from the user at
            # all. Calling it directly here instead (same fix as
            # ActionRouteBookConfirmation) means Core only ever executes
            # ONE action — this one — whose own transition to
            # action_listen IS trained (every "Route ... entry point"
            # rule ends literally at `action: action_route_entry_point`).
            return ActionShowCitiesList().run(dispatcher, tracker, domain)
        return [FollowupAction(target)]


# =========================================================================
# General task-switch — "abandon whatever form is active, jump to a
# completely different task" (user's own spec, chosen via AskUserQuestion:
# "گسترده‌تر: تعویض کامل کار وسط هر فرمی" — the BROADEST option, a general
# feature letting the user drop ANY currently-active form and jump to ANY
# of the 6 top-level tasks at any point — not just extending the existing
# change_destination_city pattern to a couple more forms).
#
# Distinct from content-drifting (_is_content_drift_trigger above, and
# ActionHandleDestinationChange/ActionStartTripFromWeatherDrift): those are
# narrow, SPECIFIC pivots the project already had (change the destination
# city within the same trip-planning/booking flow; go straight from a
# weather check into planning a trip TO that same city). This is the
# general case — e.g. "check the weather instead" typed mid
# trip_planning_form, or "actually I want to talk to a human" typed mid
# currency_exchange_form.
# =========================================================================


def _is_foreign_task_switch(tracker: Tracker, own_intents: FrozenSet[Text] = frozenset()) -> bool:
    """True exactly when the CURRENT message was classified as one of the
    6 flow-starting "entry point" intents (_ENTRY_POINT_TARGETS above) AND
    it is NOT one of `own_intents` — i.e. the user is asking for a
    DIFFERENT top-level task than whichever form is currently active, not
    just continuing/restarting this same one (own_intents is normally the
    one entry intent that activated the currently-active form, so that
    exact intent is never treated as a "foreign" switch away from it).

    Same `type: custom` bypass-of-`not_intent` root cause as
    _is_content_drift_trigger above (see its docstring for the full,
    real-execution-confirmed mechanism: domain.yml's `not_intent`
    restriction only gates the DECLARATIVE from_entity/from_text mapping
    layer, so a `type: custom` slot's own extract_<slot> method must
    check this itself). Declarative slots (num_travelers, budget_amount,
    sustainability_level, priority_focus, from_currency's/to_currency's
    from_text mapping, exchange_amount, contact_info) get the equivalent
    protection via `not_intent` extended with the 5 foreign entry
    intents instead — see domain.yml's comments on those slots.
    """
    intent = (tracker.latest_message or {}).get("intent", {}) or {}
    name = intent.get("name")
    return name in _ENTRY_POINT_TARGETS and name not in own_intents


# BUGFIX (found via this round's own real-execution verification, not
# theory): _is_foreign_task_switch alone is NOT safe to use as a bare
# top-of-function guard on a `type: custom` slot's bare requested_slot
# fallback (the one that accepts ANY non-empty text once nothing more
# specific has matched) — confirmed via a real trained-model run that a
# plain, genuinely-valid city name typed as an answer ("Wurzburg", sent
# to answer "Which city's weather do you want to check?") gets
# classified by DIET as ask_trip_planning at 93% confidence (because
# ask_trip_planning's own training data includes bare single-city
# examples like "i'm heading to paris") — an early, intent-only guard
# there wrongly discarded a perfectly good answer and bounced the user
# back into trip planning instead of accepting it.
#
# The fix mirrors this codebase's own established, proven pattern (see
# validate_travel_date_from's "ORDER" comment, and _match_city_or_ask's
# "Off-topic check ORDER" comment): try to actually resolve the raw text
# to a real answer FIRST, no matter how DIET classified the message —
# only once that genuinely fails does the message's intent get treated
# as a signal that it must be something else entirely (a foreign task
# switch). These two helpers are the "genuinely fails" check for city
# slots and date slots respectively, used ONLY at each extractor's bare
# requested_slot fallback point (trigger-word matches like "to Rome" /
# "in Rome" already require enough real structure of their own not to
# need this extra check).
def _foreign_switch_unless_real_city(tracker: Tracker, candidate: Text, own_intents: FrozenSet[Text] = frozenset()) -> bool:
    """True = block this candidate (it's a foreign task switch, and the
    raw text does NOT also happen to resolve to a real city) - the
    extractor should return {} so the form rejects and
    action_handle_task_switch gets a chance to run. False = let the
    candidate through as normal, either because this isn't a foreign
    task-switch message at all, or because it is one but the text ALSO
    genuinely matches a real city (see this function's own module-level
    comment above)."""
    if not _is_foreign_task_switch(tracker, own_intents=own_intents):
        return False
    try:
        match = api_clients.match_city(candidate)
    except api_clients.ApiError:
        match = None
    return not (match and match.get("found"))


def _foreign_switch_unless_real_date(tracker: Tracker, candidate: Text, own_intents: FrozenSet[Text] = frozenset()) -> bool:
    """Same idea as _foreign_switch_unless_real_city, for date slots —
    see that function's docstring and the shared module-level comment
    above it."""
    if not _is_foreign_task_switch(tracker, own_intents=own_intents):
        return False
    return date_resolver.resolve_date(candidate) is None


def _foreign_switch_unless_booking_answer(tracker: Tracker, candidate: Text) -> bool:
    """Same idea again, for book_confirmation_form's single yes/no slot —
    see _foreign_switch_unless_real_city's docstring. No own_intents
    here: book_confirmation_form is never itself entered via one of the
    6 entry-point intents (see extract_book_confirmation's own comment),
    so every one of them is genuinely foreign to it."""
    if not _is_foreign_task_switch(tracker):
        return False
    return not (_looks_like_booking_yes(candidate) or _looks_like_booking_no(candidate))


_TASK_SWITCH_ACK = {
    "ask_trip_planning": "Sure — let's plan a trip instead. 🧳",
    "ask_weather": "Sure — let's check the weather instead. 🌤️",
    "ask_currency_exchange": "Sure — let's do a currency exchange instead. 💱",
    "ask_human_advisor": "Sure — connecting you with a human advisor instead. 🧑‍💼",
    "see_hotel_list": "Sure — let's look at hotels instead. 🏨",
    "see_cities_list": "Sure — here's the city list instead. 🗺️",
}


class ActionHandleTaskSwitch(Action):
    """Fires when the currently active form rejects because
    _is_foreign_task_switch (see its docstring) found the message
    classified as one of the 6 flow-starting entry intents, but NOT the
    one that started the CURRENTLY active form — see rules.yml's "Task
    switch to ... during ..." rules (one per active form × foreign entry
    intent pair) for exactly which combinations route here.

    Deliberately thin, like ActionHandleDestinationChange above — reuses
    ActionRouteEntryPoint (the SAME single dynamic-routing action every
    plain entry-point rule already funnels through) instead of
    reimplementing its target lookup / awaiting_finish check /
    action_show_cities_list special-casing (see that class's own
    docstring for why a single shared routing action is used instead of
    duplicating that logic per interrupt rule).

    No `action: <form>` / `active_loop: <form>` tail on the rules that
    reach this action (unlike "Handle destination change during trip
    planning") — not needed here: ActiveLoop(None) below formally exits
    whatever loop was active, and ActionRouteEntryPoint's FollowupAction
    always activates a DIFFERENT loop than the one just exited (never the
    SAME one being re-entered), so it is constructed fresh with
    `rejected=False` by rasa's own `change_loop_to()` — the same reason
    ActionStartTripFromWeatherDrift's cross-form transition has never
    needed the self-tail fix that re-entering the SAME loop does (see the
    long root-cause comment above ActionHandleDestinationChange).

    Deliberately does NOT clear the old form's already-filled slots —
    there's no defined mapping between an arbitrary pair of forms the way
    weather_city -> destination_city is a specific, defined consumption
    (ActionStartTripFromWeatherDrift). If the user comes back to the
    abandoned task later, its own extract_<slot> methods already skip any
    slot that's still filled, so it simply resumes partway rather than
    starting completely over — a reasonable default absent any spec
    saying otherwise, and consistent with this project's existing
    "leftover slot" behaviour elsewhere.
    """

    def name(self) -> Text:
        return "action_handle_task_switch"

    def run(
        self, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict[Text, Any]
    ) -> List[EventType]:
        intent = (tracker.latest_message or {}).get("intent", {}).get("name")
        ack = _TASK_SWITCH_ACK.get(intent)
        if ack:
            dispatcher.utter_message(text=ack)
        events: List[EventType] = [ActiveLoop(None)] + reset_fallback_events()
        events += ActionRouteEntryPoint().run(dispatcher, tracker, domain)
        return events


class ActionFinish(Action):
    """Finish (the button or a typed "finish" / "I'm done") ends the
    process and clears everything, exactly as if "Clear conversation" had
    been pressed: no closing message, the tracker is wiped
    (Restarted() also resets awaiting_finish and the fallback counters),
    and the reset_to_menu marker makes the frontend clear its own chat and
    start a fresh session. The marker is needed because typed text can't
    be recognised by the frontend before it is sent, unlike the "/finish"
    button payload (same approach as ActionDenyBooking)."""

    def name(self) -> Text:
        return "action_finish"

    def run(
        self, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict[Text, Any]
    ) -> List[EventType]:
        dispatcher.utter_message(json_message={"card_type": "reset_to_menu"})
        return [Restarted()]


# =========================================================================
# Random city-button helpers (Trip Assistant Q1/Q2, Currency Exchange Q1)
# =========================================================================

# Fixed, deliberately DISJOINT lists — chosen over live-sampled buttons
# for reliability: the earlier random-sampling version called
# data_services (via api_clients.list_cities()) every time this question
# was asked, with no error handling — if data_services was briefly slow
# or unreachable right when the bot needed to ask this question, the
# whole action would crash instead of showing the question. A fixed
# list needs no network call at all. Being disjoint from each other also
# removes the need for any "exclude the destination from origin's list"
# logic — there's never an overlap to exclude in the first place.
_DESTINATION_CITY_OPTIONS = ["London", "New York", "Washington", "Berlin", "Paris", "Tokyo", "Dubai", "Sydney"]
_ORIGIN_CITY_OPTIONS = ["Rome", "Madrid", "Amsterdam", "Vienna", "Toronto", "Singapore", "Istanbul", "Cairo", "Mumbai", "Seoul"]


def _awaiting_confirmation_for(tracker: Tracker, slot_name: Text) -> bool:
    """True if this exact slot currently has an unanswered "Did you
    mean X?" confirmation pending (see _match_city_or_ask). When True,
    the custom `action_ask_<slot>` for that slot must print NOTHING —
    _match_city_or_ask's dispatcher.utter_message() already sent a
    complete, self-contained confirmation message (its own Yes button,
    its own "if not, type the right one" instruction) for this exact
    turn. Without this check, Rasa's normal forms behaviour (a failed
    validation makes the form re-ask the same slot) immediately prints
    the ORIGINAL question right after the confirmation, so the
    confirmation — and its Yes button — can end up buried under it."""
    return tracker.get_slot("pending_city_field") == slot_name


def _structured_payload_value(tracker: Tracker, slot_name: Text) -> Optional[Text]:
    """BUGFIX (real user report + real debug log - "Weather Checking":
    clicking the "Tomorrow" quick-reply button after picking a city
    printed "Sorry, that doesn't seem to answer what I just asked" even
    though typing "tomorrow" by hand worked fine; confirmed via real
    execution to affect every OTHER quick-pick button built the same way
    too - the destination_city/origin_city city-grid buttons at the very
    start of Trip Assistant, and the travel_date_from date buttons).

    A quick-reply button whose payload is a structured
    `/some_intent{"<slot_name>": "<value>"}` message (e.g.
    `/inform_dates{"weather_day_text": "tomorrow"}`, or
    `/ask_trip_planning{"destination_city": "London"}`) is, itself, the
    entire and only valid way that button can answer this slot. But
    every extract_<slot> method in this project (for a `type:
    custom`-mapped slot) starts by unconditionally blocking ANY
    "/"-prefixed message text, specifically so an unrelated structured
    payload - a city-confirmation "Yes" click, a nav-menu button, an
    entirely different flow's payload - is never mistaken for this
    slot's own literal text. That's the correct default, but it also
    blocked this slot's OWN legitimate button, since the mapping is
    `type: custom` and there is no other extraction path to fall back
    on: validate_<slot> was never even called, Core saw the form make
    zero progress this turn, logged "Execution of '<form>' was
    rejected. Setting its confidence to 0.0 in all predictions.", and
    fell through to action_smart_fallback - confirmed via a real debug
    log and reproduced via a real trained model for every button listed
    above.

    Rasa Core's own RegexMessageHandler already parses a structured
    payload's JSON body into `tracker.latest_message`'s entities BEFORE
    the action server ever runs (confirmed via the real debug log:
    clicking "Tomorrow" produced `entities=[{'entity':
    'weather_day_text', 'value': 'tomorrow', ...}]`) - regardless of
    whether this slot's domain.yml mapping is one that would otherwise
    consume it. Reading the value straight out of those entities
    (rather than hand-parsing the raw payload text ourselves) is safe
    precisely because an entity literally named `slot_name` can only
    appear here when a payload was deliberately built to carry THAT
    slot's value - no other button in this project's
    domain.yml/actions.py uses that JSON key, so this can never
    accidentally fire for some unrelated "/"-prefixed payload (a
    Yes-click, a nav button, a different flow's entry point, ...)."""
    for entity in (tracker.latest_message or {}).get("entities") or []:
        if entity.get("entity") == slot_name and entity.get("value") not in (None, ""):
            return str(entity["value"])
    return None


class ActionAskDestinationCity(Action):
    """Custom `action_ask_<slot>` — Rasa Core calls this INSTEAD of a
    static utter_ask_destination_city response (none is defined for this
    slot in domain.yml) whenever the form needs to ask it. Only reason
    this needs to be a custom action rather than a plain response is
    that it's the FIRST of two DIFFERENT button sets shown across Q1/Q2
    — see ActionAskOriginCity below."""

    def name(self) -> Text:
        return "action_ask_destination_city"

    def run(
        self, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict[Text, Any]
    ) -> List[EventType]:
        if _awaiting_confirmation_for(tracker, "destination_city"):
            return []
        dispatcher.utter_message(
            text="Where would you like to travel to? (e.g: to Rome, to Tehran, …)",
            buttons=[
                {"title": c, "payload": f'/ask_trip_planning{{"destination_city": "{c}"}}'}
                for c in _DESTINATION_CITY_OPTIONS
            ],
        )
        return []


class ActionAskOriginCity(Action):
    def name(self) -> Text:
        return "action_ask_origin_city"

    def run(
        self, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict[Text, Any]
    ) -> List[EventType]:
        if _awaiting_confirmation_for(tracker, "origin_city"):
            return []
        dispatcher.utter_message(
            text="Where would you like to travel from? (e.g: from Rome, from Tehran, …)",
            buttons=[
                {"title": c, "payload": f'/ask_trip_planning{{"origin_city": "{c}"}}'}
                for c in _ORIGIN_CITY_OPTIONS
            ],
        )
        return []


class ActionAskWeatherCity(Action):
    """Was a plain `utter_ask_weather_city` response — converted to a
    custom action for the same reason as the two above: it needs to
    stay silent while a typo confirmation for this slot is pending."""

    def name(self) -> Text:
        return "action_ask_weather_city"

    def run(
        self, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict[Text, Any]
    ) -> List[EventType]:
        if _awaiting_confirmation_for(tracker, "weather_city"):
            return []
        dispatcher.utter_message(text="Which city's weather do you want to check? (Paris, Berlin, New York, Tehran, …)")
        return []


class ActionAskHotelListCity(Action):
    def name(self) -> Text:
        return "action_ask_hotel_list_city"

    def run(
        self, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict[Text, Any]
    ) -> List[EventType]:
        if _awaiting_confirmation_for(tracker, "hotel_list_city"):
            return []
        dispatcher.utter_message(text="Which city would you like to see hotels for?")
        return []


_DATE_OPTION_LABELS = ["Tomorrow", "This weekend", "Next weekend", "Next Sunday", "Next Monday", "Next week", "In two weeks"]
_DATE_OPTION_VALUES = ["tomorrow", "this weekend", "next weekend", "next Sunday", "next Monday", "next week", "in two weeks"]


class ActionAskTravelDateFrom(Action):
    def name(self) -> Text:
        return "action_ask_travel_date_from"

    def run(
        self, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict[Text, Any]
    ) -> List[EventType]:
        dispatcher.utter_message(
            text="When are you going to leave? (Select one of the buttons or provide a specific date like 2026/12/16.)",
            buttons=[
                {"title": label, "payload": f'/inform_dates{{"travel_date_from": "{value}"}}'}
                for label, value in zip(_DATE_OPTION_LABELS, _DATE_OPTION_VALUES)
            ],
        )
        return []


_CURRENCY_PAIRS = [
    ("EUR", "USD"), ("USD", "EUR"), ("EUR", "GBP"), ("GBP", "EUR"), ("USD", "JPY"),
    ("JPY", "USD"), ("USD", "GBP"), ("GBP", "USD"), ("EUR", "CHF"), ("USD", "CAD"),
]


class ActionAskFromCurrency(Action):
    def name(self) -> Text:
        return "action_ask_from_currency"

    def run(
        self, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict[Text, Any]
    ) -> List[EventType]:
        dispatcher.utter_message(
            text="Which currency to which currency?",
            buttons=[
                {"title": f"{a} → {b}", "payload": f'/ask_currency_exchange{{"from_currency": "{a}", "to_currency": "{b}"}}'}
                for a, b in _CURRENCY_PAIRS
            ],
        )
        return []


# =========================================================================
# Trip Assistant & Booking — trip_planning_form (Q1-Q8)
# =========================================================================

# BUGFIX (real user report): "دنبال تاریخ‌های رفت و برگشت هم بگرده... با
# from شروع میشن ... و از to برای تعیین تاریخ برگشت استفاده میکنن" — the
# same "from X to Y" combined-message habit already supported for cities
# (extract_destination_city/extract_origin_city above) should work for
# the departure/return DATES too: "from 2026/12/10 to 2026/12/16" said in
# one message should fill BOTH travel_date_from and travel_date_to,
# instead of the bot re-asking each one separately.
#
# Deliberately a much simpler split than city_extractor's word-capped
# regex: a date phrase isn't bounded to 1-2 words the way a city name is
# ("the day after tomorrow", "16th of December" are all valid), so this
# takes the FULL span between "from" and "to" as the departure candidate,
# and the (bounded — see below) remaining span after "to" as the return
# candidate, then hands each to date_resolver.resolve_date to decide
# whether it's actually a recognisable date at all. That resolve step is
# what keeps this safe to run unconditionally (see both extractors'
# docstrings below) — a non-date phrase in that position (e.g. "from
# Berlin to Paris", a CITY sentence, not a date one) simply fails to
# resolve and is ignored, exactly like city_extractor's own date-phrase
# rejection guards against the reverse mistake (a date phrase being
# mistaken for a city).
#
# BUGFIX (real user report, confirmed via real execution): "I want to go
# to a travel from tomorrow to next week from berlin to lisbon" — one
# message combining BOTH a date range AND a city range — filled
# travel_date_to with the literal garbage string "next week from berlin
# to lisbon" instead of just "next week". Root cause: the return-phrase
# group used to be `(.+)`, greedy with no end boundary other than the
# end of the string, so once it matched past the date range's own "to"
# it kept going and swallowed the SECOND, unrelated "from ... to ..."
# clause (the city range) as well. date_resolver.resolve_date then
# "resolved" that whole garbled string anyway, because its relative-
# phrase checks use re.search (looking for a phrase ANYWHERE in the
# text, e.g. "next week"), not a full-string match — so the bad
# candidate silently passed validation and got stored verbatim.
# The return-phrase group is now non-greedy and bounded to stop at the
# next standalone "from" (a second "from" is a reliable signal that an
# unrelated clause — almost always the city range — starts there), or
# at the end of the string if no such second "from" exists. A genuine
# single date range like "from 2026/12/10 to 2026/12/16" has no second
# "from" at all, so it is completely unaffected by this change.
_DATE_RANGE_PATTERN = re.compile(
    r"(?i:\bfrom\b)\s+(.+?)\s+(?i:\bto\b)\s+(.+?)(?=\s+(?i:from)\b|$)"
)


def _extract_date_range_candidates(text: Text) -> "tuple[Optional[str], Optional[str]]":
    """Split "from <phrase> to <phrase>" into (departure_phrase,
    return_phrase) candidates — NOT yet validated as real dates, that's
    the caller's job (see extract_travel_date_from/_to below). Returns
    (None, None) if the message doesn't have this shape at all."""
    match = _DATE_RANGE_PATTERN.search(text)
    if not match:
        return None, None
    departure = match.group(1).strip().rstrip(",.!?;:")
    return_phrase = match.group(2).strip().rstrip(",.!?;:")
    return (departure or None), (return_phrase or None)


# BUGFIX (real user report, confirmed via real debug log from the user's own
# deployment): "reserve a flight to there from Berlin" then, in reply to
# "When are you going to leave?", "this weekend to next weekend" — no
# leading "from" — was NOT split at all (_DATE_RANGE_PATTERN above requires
# one), so the entire raw string was swallowed whole into travel_date_from
# by the single-slot fallback further down, and date_resolver.resolve_date's
# find-anywhere-in-string phrase matching silently keyed off only the FIRST
# relative-date phrase ("this weekend") and ignored the rest. The bot then
# asked a redundant "And when are you going to come back?" instead of
# recognizing the combined range.
#
# Fix: a second, narrower pattern for a bare "<phrase> to <phrase>" with no
# "from" — tried by the caller ONLY when _extract_date_range_candidates
# above already found nothing. It has no strong keyword like "from" to
# anchor on, so on its own it would too easily mis-split an ordinary
# sentence that merely contains the word "to" somewhere (e.g. "I want to
# travel to Rome next week" would split into "I want" / "travel to Rome
# next week"). Two things keep that safe:
#   1. The pattern is anchored to the ENTIRE message (^...$), matching how
#      the user actually typed it — just the range, nothing else.
#   2. Unlike the "from" pattern (where either half resolving on its own is
#      enough — see extract_travel_date_from/_to below), a bare-pattern
#      candidate is only ever accepted once the caller has confirmed BOTH
#      halves independently resolve via date_resolver.resolve_date. A real
#      range reads as two dates on either side of "to"; a sentence that
#      merely contains the word "to" essentially never does. ("I want" /
#      "travel to Rome next week" fails this: "I want" resolves to nothing,
#      so the whole candidate pair is rejected.)
# As an extra guard, the caller only tries this fallback when the bot is
# actively asking about one of the two date slots (requested_slot is
# travel_date_from or travel_date_to) — exactly the context this phrasing
# occurs in — so it never fires on an unrelated turn (e.g. answering
# destination_city) even before the both-halves-resolve check runs.
_DATE_RANGE_PATTERN_BARE = re.compile(r"^\s*(.+?)\s+(?i:\bto\b)\s+(.+?)\s*$")


def _extract_bare_date_range_candidates(text: Text) -> "tuple[Optional[str], Optional[str]]":
    """Fallback split for a combined range with no leading "from" — see the
    BUGFIX comment above _DATE_RANGE_PATTERN_BARE. Only ever called by the
    caller when _extract_date_range_candidates already returned nothing,
    and its result is only trustworthy once the caller has independently
    confirmed BOTH halves resolve via date_resolver.resolve_date — this
    function only performs the (unvalidated) split."""
    match = _DATE_RANGE_PATTERN_BARE.match(text)
    if not match:
        return None, None
    departure = match.group(1).strip().rstrip(",.!?;:")
    return_phrase = match.group(2).strip().rstrip(",.!?;:")
    return (departure or None), (return_phrase or None)


class ValidateTripPlanningForm(DedupFormValidation):
    def name(self) -> Text:
        return "validate_trip_planning_form"

    async def required_slots(
        self,
        domain_slots: List[Text],
        dispatcher: CollectingDispatcher,
        tracker: Tracker,
        domain: Dict[Text, Any],
    ) -> List[Text]:
        # Adaptive branch: travellers who said sustainability is their TOP
        # priority get one extra question that decides how hard the scoring
        # function (scoring.py) leans on carbon vs. cost.
        slots = list(domain_slots)  # copy — never mutate what we were given
        if tracker.get_slot("sustainability_level") != "high" and "priority_focus" in slots:
            slots.remove("priority_focus")
        return slots

    # ---- custom extraction: regex over the raw message, not just DIET's
    # entities — see actions/city_extractor.py.
    async def extract_destination_city(
        self, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict[Text, Any]
    ) -> Dict[Text, Any]:
        if tracker.get_slot("destination_city") is not None:
            return {}
        text = (tracker.latest_message or {}).get("text") or ""
        if text.startswith("/"):
            # BUGFIX (real user report + real debug log, "Yes"-click
            # rejected the form instead of resolving the pending city):
            # a "/"-prefixed payload is usually a button click for an
            # UNRELATED intent and must not be treated as a city name —
            # but when this exact slot has its own fuzzy-match "Did you
            # mean X?" confirmation pending (_awaiting_confirmation_for),
            # the "Yes" button's raw "/affirm_city_suggestion" payload
            # MUST still reach validate_destination_city, because that's
            # the ONLY place the pending suggestion actually gets
            # resolved (see _match_city_or_ask's top branch). This slot's
            # domain.yml mapping is `type: custom` — there is no OTHER
            # extraction path (no from_text/from_entity fallback) to
            # pick it up if this method blocks it. Blocking it here
            # unconditionally meant the slot was never extracted at all
            # on the Yes-click turn, so validate_destination_city was
            # never even called, Core saw the form make zero progress,
            # and (confirmed via a real debug log) logged "Execution of
            # '...' was rejected. Setting its confidence to 0.0 in all
            # predictions." — falling through to action_smart_fallback's
            # "Sorry, that doesn't seem to answer what I just asked"
            # instead of resolving to the suggested city.
            if _awaiting_confirmation_for(tracker, "destination_city"):
                return {"destination_city": text.strip()}
            # BUGFIX (real user report, confirmed via real execution):
            # see _structured_payload_value's docstring — this is what
            # makes ActionAskDestinationCity's own city-grid buttons
            # (payload `/ask_trip_planning{"destination_city": "..."}`)
            # actually work, instead of being silently blocked by the
            # early return above and rejecting the whole form.
            structured_value = _structured_payload_value(tracker, "destination_city")
            if structured_value is not None:
                return {"destination_city": structured_value}
            return {}
        trigger_match = city_extractor.extract_destination_trigger(text)
        if trigger_match:
            return {"destination_city": trigger_match}
        # Lenient, any-case "to X" match (see city_extractor's module
        # docstring) — real user report: a casual, all-lowercase
        # sentence like "i wanna go to dubai" wasn't recognised at all
        # by the strict, Capital-Letter-only extractor above.
        #
        # BUGFIX (real user report): this used to only run when
        # destination_city was ALREADY the requested_slot, which meant
        # a combined lowercase sentence like "i want to go to frankfurt
        # from berlin" — sent as the FIRST answer, while destination_city
        # IS being asked, so this part actually did fire — worked for
        # destination, but origin_city's OWN lenient match (below) was
        # blocked from running on this SAME message (requested_slot was
        # still "destination_city", not "origin_city" yet), so "berlin"
        # was silently lost. Now unconditional here too (matching
        # origin_city's fix below) so this is symmetric and either slot
        # can be found regardless of which one Rasa happens to be
        # asking about at that instant — safe because it requires an
        # explicit "to X" trigger word, confirmed via city_extractor's
        # own docstring reasoning. Only the BARE whole-message fallback
        # (no trigger word matched at all) still needs the exact-slot
        # gate, since that guess isn't safe to apply to just any message.
        lenient_match = city_extractor.extract_destination_trigger_lenient(text)
        if lenient_match and lenient_match.strip().lower() not in _PLACE_PRONOUNS:
            return {"destination_city": lenient_match}
        if tracker.get_slot("requested_slot") == "destination_city":
            candidate = text.strip()
            if not candidate:
                return {}
            # See _foreign_switch_unless_real_city's docstring — this
            # slot's `type: custom` mapping bypasses domain.yml's
            # `not_intent` entirely, so a foreign task-switch message
            # ("check the weather instead") must be caught here too, but
            # ONLY once a real city lookup on the raw text has already
            # failed — a plain, genuinely valid city name typed at Q1
            # (destination_city itself pending) can otherwise be
            # misclassified by DIET as e.g. ask_trip_planning (confirmed
            # via real execution) and must still be accepted. Unlike
            # change_destination_city (deliberately NOT guarded at all
            # here — see this form's other extractors' comments — naming
            # a NEW destination at Q1 degrades correctly into just
            # answering Q1), a genuinely foreign task-switch phrase has
            # nothing to do with providing a city name, so once THIS
            # check confirms the text isn't a real city either, it's
            # correctly left for action_handle_task_switch to handle.
            if _foreign_switch_unless_real_city(tracker, candidate, own_intents=frozenset({"ask_trip_planning"})):
                return {}
            return {"destination_city": candidate}
        return {}

    async def extract_origin_city(
        self, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict[Text, Any]
    ) -> Dict[Text, Any]:
        if tracker.get_slot("origin_city") is not None:
            return {}
        # See _is_content_drift_trigger's docstring — this slot's `type:
        # custom` mapping bypasses domain.yml's `not_intent` entirely, so
        # without this, "actually, change the destination to Vienna"
        # sent while origin_city is still unfilled (Q2) would fall
        # through to this method's own bare requested_slot fallback below
        # and get swallowed as a (bogus) origin-city candidate, instead
        # of leaving this slot empty so the form rejects and
        # action_handle_destination_change gets a chance to run.
        if _is_content_drift_trigger(tracker, "change_destination_city"):
            return {}
        text = (tracker.latest_message or {}).get("text") or ""
        if text.startswith("/"):
            # See extract_destination_city's comment above — same fix,
            # same real bug (a pending "Yes" confirmation for THIS slot
            # must still reach validate_origin_city, or the form gets
            # rejected by Core instead of resolving the suggested city).
            if _awaiting_confirmation_for(tracker, "origin_city"):
                return {"origin_city": text.strip()}
            # See extract_destination_city's comment above — same fix,
            # for ActionAskOriginCity's own city-grid buttons (payload
            # `/ask_trip_planning{"origin_city": "..."}`).
            structured_value = _structured_payload_value(tracker, "origin_city")
            if structured_value is not None:
                return {"origin_city": structured_value}
            return {}
        trigger_match = city_extractor.extract_origin_trigger(text)
        if trigger_match:
            return {"origin_city": trigger_match}
        # See extract_destination_city's comment above — same lenient,
        # any-case "from X" fallback, ALSO unconditional now for the
        # same reason (real, reproduced bug: "i want to go to frankfurt
        # from berlin" sent as the first message left origin_city
        # unfilled because requested_slot was still "destination_city"
        # at that instant, even though "from berlin" was right there).
        lenient_match = city_extractor.extract_origin_trigger_lenient(text)
        if lenient_match:
            return {"origin_city": lenient_match}
        if tracker.get_slot("requested_slot") == "origin_city":
            candidate = text.strip()
            if not candidate:
                return {}
            # See _foreign_switch_unless_real_city's docstring — same
            # additional guard as extract_destination_city above, same
            # reason (this slot's `type: custom` mapping bypasses
            # domain.yml's `not_intent` entirely, and a real city name
            # can be misclassified as a foreign entry intent by DIET).
            if _foreign_switch_unless_real_city(tracker, candidate, own_intents=frozenset({"ask_trip_planning"})):
                return {}
            return {"origin_city": candidate}
        return {}

    async def extract_travel_date_from(
        self, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict[Text, Any]
    ) -> Dict[Text, Any]:
        if tracker.get_slot("travel_date_from") is not None:
            return {}
        # See _is_content_drift_trigger's docstring — same `type: custom`
        # bypass-of-not_intent fix as extract_origin_city above.
        if _is_content_drift_trigger(tracker, "change_destination_city"):
            return {}
        text = (tracker.latest_message or {}).get("text") or ""
        if text.startswith("/"):
            # BUGFIX (real user report + real debug log — the same bug
            # class as weather_day_text's day-suggestion buttons, found
            # while auditing every similar button per the user's own
            # request): ActionAskTravelDateFrom's own quick-reply
            # buttons ("Tomorrow", "This weekend", ...) send a
            # structured `/inform_dates{"travel_date_from": "..."}`
            # payload — the ONLY way those buttons can ever answer this
            # question — but this early return was blocking it
            # unconditionally, and travel_date_from's `type: custom`
            # mapping means there's no other extraction path to pick it
            # up instead. See _structured_payload_value's docstring for
            # the full root cause.
            structured_value = _structured_payload_value(tracker, "travel_date_from")
            if structured_value is not None:
                return {"travel_date_from": structured_value}
            return {}
        # BUGFIX (real user report): "from 2026/12/10 to 2026/12/16" (or
        # "from next Monday to next Friday", ...) sent in one message —
        # try the "from <phrase> to <phrase>" split first. Safe to run
        # unconditionally: the departure candidate is only ever used if
        # date_resolver can actually resolve it to a real date, so a
        # non-date "from X to Y" sentence (e.g. a combined cities
        # message, "from Berlin to Paris") is never mistaken for one —
        # see _extract_date_range_candidates' module-level comment.
        departure, _ = _extract_date_range_candidates(text)
        if departure and date_resolver.resolve_date(departure) is not None:
            return {"travel_date_from": departure}
        requested_slot = tracker.get_slot("requested_slot")
        # BUGFIX (real user report — see _DATE_RANGE_PATTERN_BARE's
        # comment above): no leading "from" was found above, so try the
        # bare "<phrase> to <phrase>" fallback, gated to only when the bot
        # is actively asking about one of the two date slots and only
        # accepted once BOTH halves independently resolve as real dates.
        if requested_slot in ("travel_date_from", "travel_date_to"):
            bare_departure, bare_return = _extract_bare_date_range_candidates(text)
            if (
                bare_departure
                and bare_return
                and date_resolver.resolve_date(bare_departure) is not None
                and date_resolver.resolve_date(bare_return) is not None
            ):
                return {"travel_date_from": bare_departure}
        if requested_slot == "travel_date_from":
            candidate = text.strip()
            if not candidate:
                return {}
            # See _foreign_switch_unless_real_date's docstring — same
            # idea as extract_destination_city's
            # _foreign_switch_unless_real_city guard, for date slots (a
            # genuinely valid date phrase can, in principle, be
            # misclassified as a foreign entry intent the same way a
            # city name can, so it must still be accepted if it actually
            # resolves to a real date).
            if _foreign_switch_unless_real_date(tracker, candidate, own_intents=frozenset({"ask_trip_planning"})):
                return {}
            return {"travel_date_from": candidate}
        return {}

    async def extract_travel_date_to(
        self, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict[Text, Any]
    ) -> Dict[Text, Any]:
        if tracker.get_slot("travel_date_to") is not None:
            return {}
        # See _is_content_drift_trigger's docstring — same `type: custom`
        # bypass-of-not_intent fix as extract_origin_city above.
        if _is_content_drift_trigger(tracker, "change_destination_city"):
            return {}
        text = (tracker.latest_message or {}).get("text") or ""
        if text.startswith("/"):
            # See extract_travel_date_from's comment above — same fix,
            # applied uniformly (no direct quick-pick button exists for
            # this slot today, but this guards against the same
            # rejection if one is ever added).
            structured_value = _structured_payload_value(tracker, "travel_date_to")
            if structured_value is not None:
                return {"travel_date_to": structured_value}
            return {}
        # Mirror image of extract_travel_date_from above — the "to
        # <phrase>" half of the same "from X to Y" combined sentence.
        _, return_phrase = _extract_date_range_candidates(text)
        if return_phrase and date_resolver.resolve_date(return_phrase) is not None:
            return {"travel_date_to": return_phrase}
        requested_slot = tracker.get_slot("requested_slot")
        # Mirror image of extract_travel_date_from's same fix above — see
        # _DATE_RANGE_PATTERN_BARE's comment for the full explanation.
        if requested_slot in ("travel_date_from", "travel_date_to"):
            bare_departure, bare_return = _extract_bare_date_range_candidates(text)
            if (
                bare_departure
                and bare_return
                and date_resolver.resolve_date(bare_departure) is not None
                and date_resolver.resolve_date(bare_return) is not None
            ):
                return {"travel_date_to": bare_return}
        if requested_slot == "travel_date_to":
            candidate = text.strip()
            if not candidate:
                return {}
            # See extract_travel_date_from's comment above — same
            # additional guard, same reason.
            if _foreign_switch_unless_real_date(tracker, candidate, own_intents=frozenset({"ask_trip_planning"})):
                return {}
            return {"travel_date_to": candidate}
        return {}

    def validate_destination_city(
        self, slot_value: Any, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict[Text, Any]
    ) -> Dict[Text, Any]:
        return _match_city_or_ask("destination_city", str(slot_value), dispatcher, tracker)

    def validate_origin_city(
        self, slot_value: Any, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict[Text, Any]
    ) -> Dict[Text, Any]:
        result = _match_city_or_ask("origin_city", str(slot_value), dispatcher, tracker)
        confirmed = result.get("origin_city")
        if confirmed is None:
            return result  # "not found" or "pending confirmation" — pass through unchanged

        destination = tracker.get_slot("destination_city")
        if destination and confirmed.strip().lower() == str(destination).strip().lower():
            dispatcher.utter_message(
                text="Your origin can't be the same as your destination — where are you actually travelling from?"
            )
            return {"origin_city": None}
        return result  # includes pending_city_field/pending_city_suggestion cleanup when this came from a "Yes" click

    def validate_travel_date_from(
        self, slot_value: Any, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict[Text, Any]
    ) -> Dict[Text, Any]:
        # ORDER (real user report, "tone down" the off-topic gating):
        # try to actually resolve a date FIRST — if it resolves, accept
        # it no matter how DIET classified the message. Off-topic is
        # only checked once date parsing has genuinely failed, so a
        # valid but narrowly-trained-for date phrase (which is exactly
        # what tends to get DIET's least confident, nlu_fallback
        # classification) is never wrongly treated as an unrelated
        # message — see domain.yml's matching "not_intent SCOPE" note.
        text = str(slot_value).strip()
        if text and len(text) <= 100 and date_resolver.resolve_date(text) is not None:
            return {"travel_date_from": text}

        offtopic = _offtopic_fallback_check("travel_date_from", dispatcher, tracker)
        if offtopic is not None:
            return offtopic

        if not text or len(text) > 100:
            dispatcher.utter_message(text="Sorry, could you give me a rough departure date, in a few words?")
        else:
            dispatcher.utter_message(
                text=f"I couldn't work out a date from '{text}' — try some date like \"2026-12-16\"."
            )
        return {"travel_date_from": None}

    def validate_travel_date_to(
        self, slot_value: Any, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict[Text, Any]
    ) -> Dict[Text, Any]:
        # See validate_travel_date_from's ORDER comment above — same
        # parse-first, off-topic-check-only-on-failure approach.
        text = str(slot_value).strip()

        from_text = tracker.get_slot("travel_date_from")
        resolved_from = date_resolver.resolve_date(from_text) if from_text else None

        # BUGFIX (real user report + screenshot — the travel_date_to
        # question was "totally confusing" and looped): a relative
        # phrase here ("weekend", "next week", "next Monday", "tomorrow",
        # "in two weeks", ...) was being resolved relative to TODAY (the
        # default reference when none is passed), not relative to the
        # already-answered departure date. So for anyone whose departure
        # date is more than a few days out — which is most real trips —
        # "next week" or "the weekend" meant a date that had already
        # passed relative to the DEPARTURE date, and the nights_between
        # check below rejected it every single time as "before your
        # departure date", forever, no matter what relative phrase was
        # typed. That's the actual root cause of the reported loop, not
        # a parsing failure — "weekend" (and every other relative
        # phrase) parsed FINE, it just parsed relative to the wrong day.
        #   The natural reading of "I leave Dec 20, back next week" is
        # obviously "the week after I leave", not "next week from
        # today" — so relative phrases here are now resolved with the
        # DEPARTURE date as the reference point instead of today
        # (falling back to today only if travel_date_from itself hasn't
        # resolved to anything yet). This doesn't change absolute dates
        # ("2026/12/16") at all — dateutil parses those the same way
        # regardless of reference.
        resolved_to = (
            date_resolver.resolve_date(text, reference=resolved_from or date_cls.today())
            if text and len(text) <= 100 else None
        )

        if resolved_to is not None:
            if resolved_from and date_resolver.nights_between(resolved_from, resolved_to) is None:
                # Edge case surfaced by real testing: when the DEPARTURE
                # date itself already falls on a Saturday/Sunday, "the
                # weekend" (bare, or "this weekend") resolves — correctly,
                # for a departure-date reading — to that SAME day, since
                # that branch of date_resolver.resolve_date treats an
                # already-weekend reference as "now". For a RETURN date
                # that produces a same-day, 0-night result, which isn't
                # what anyone means by "back that weekend" when they're
                # already leaving that day. Rather than reject outright
                # and force the user to guess a different phrase, retry
                # the SAME phrase once more, anchored one day further out
                # — rolling a same-day "weekend"/"next week"/etc into the
                # NEXT occurrence — before giving up.
                retry_to = date_resolver.resolve_date(text, reference=resolved_from + timedelta(days=1))
                if retry_to and date_resolver.nights_between(resolved_from, retry_to) is not None:
                    return {"travel_date_to": text}
                dispatcher.utter_message(text="Your return date needs to be after your departure date — could you try again?")
                return {"travel_date_to": None}
            return {"travel_date_to": text}

        offtopic = _offtopic_fallback_check("travel_date_to", dispatcher, tracker)
        if offtopic is not None:
            return offtopic

        if not text or len(text) > 100:
            dispatcher.utter_message(text="Sorry, could you give me a rough return date, in a few words?")
        else:
            dispatcher.utter_message(
                text=f"I couldn't work out a date from '{text}' — try some date like \"2026-12-23\"."
            )
        return {"travel_date_to": None}

    # Common ways to say "just one traveller" without a number at all —
    # real user report: "only me", "just me", "me" etc. should fill this
    # slot with 1, the same as clicking the "Just me" button.
    _SOLO_TRAVELER_PHRASES = {
        "me", "just me", "only me", "it's just me", "its just me",
        "it's only me", "its only me", "just myself", "only myself",
        "myself", "solo", "by myself", "alone", "just the one of us",
        "one person", "1 person", "solo trip", "solo traveller", "solo traveler",
    }

    def validate_num_travelers(
        self, slot_value: Any, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict[Text, Any]
    ) -> Dict[Text, Any]:
        # ORDER (see validate_travel_date_from's comment for the general
        # reasoning) — try to actually parse an answer FIRST, whether
        # that's a number or a solo-traveller phrase, before treating
        # anything as off-topic.
        normalised = str(slot_value).strip().lower().rstrip(",.!?;:")
        if normalised in self._SOLO_TRAVELER_PHRASES:
            return {"num_travelers": 1}

        try:
            n = int(float(slot_value))
            if not (1 <= n <= 20):
                raise ValueError
            return {"num_travelers": n}
        except (TypeError, ValueError):
            pass

        offtopic = _offtopic_fallback_check("num_travelers", dispatcher, tracker)
        if offtopic is not None:
            return offtopic

        dispatcher.utter_message(text="Please give me a number of travellers between 1 and 20.")
        return {"num_travelers": None}

    _NUMBER_PATTERN = re.compile(r"\d[\d,]*\.?\d*")

    def validate_budget_amount(
        self, slot_value: Any, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict[Text, Any]
    ) -> Dict[Text, Any]:
        # ORDER (real user report: typing a bare "5000" or "4500" for
        # the budget was being rejected — see validate_travel_date_from's
        # comment for the general reasoning). Parses FIRST; off-topic is
        # only checked once parsing has genuinely failed.
        #
        # Handles a plain number ("800"), a number embedded in a sentence
        # ("my budget is 800 EUR"), AND a range ("500 to 800", "EUR350 - EUR850")
        # — a range is reduced to its midpoint, since that's the single
        # number scoring.py's ranking needs.
        numbers = [float(n.replace(",", "")) for n in self._NUMBER_PATTERN.findall(str(slot_value))]
        if numbers:
            amount = (numbers[0] + numbers[1]) / 2 if len(numbers) >= 2 else numbers[0]
            if amount > 0:
                return {"budget_amount": amount}
            dispatcher.utter_message(text="I need a positive number for your budget.")
            return {"budget_amount": None}

        offtopic = _offtopic_fallback_check("budget_amount", dispatcher, tracker)
        if offtopic is not None:
            return offtopic

        dispatcher.utter_message(text='I need a number for your budget — e.g. "800" or "800 EUR".')
        return {"budget_amount": None}

    _SUSTAINABILITY_KEYWORDS = {
        "high": ("high", "lot", "most", "top priorit", "green", "eco", "sustainab", "carbon", "environment"),
        "low": ("low", "cheap", "budget", "cost", "price", "money"),
        "medium": ("medium", "balance", "mix", "middle", "both", "moderate"),
    }
    _PRIORITY_FOCUS_KEYWORDS = {
        "carbon": ("carbon", "green", "emission", "eco"),
        "cost": ("cost", "cheap", "price", "money", "budget"),
        "balanced": ("balance", "mix", "both", "middle"),
    }

    def validate_sustainability_level(
        self, slot_value: Any, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict[Text, Any]
    ) -> Dict[Text, Any]:
        # ORDER — see validate_travel_date_from's comment. Parse first.
        value = str(slot_value).strip().lower()
        if value in ("low", "medium", "high"):
            return {"sustainability_level": value}
        for label, keywords in self._SUSTAINABILITY_KEYWORDS.items():
            if any(kw in value for kw in keywords):
                return {"sustainability_level": label}

        offtopic = _offtopic_fallback_check("sustainability_level", dispatcher, tracker)
        if offtopic is not None:
            return offtopic

        dispatcher.utter_message(
            text='Please pick one of the options below, or describe it in your own words (e.g. "sustainability matters most to me").'
        )
        return {"sustainability_level": None}

    def validate_priority_focus(
        self, slot_value: Any, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict[Text, Any]
    ) -> Dict[Text, Any]:
        # ORDER — see validate_travel_date_from's comment. Parse first.
        value = str(slot_value).strip().lower()
        if value in ("carbon", "cost", "balanced"):
            return {"priority_focus": value}
        for label, keywords in self._PRIORITY_FOCUS_KEYWORDS.items():
            if any(kw in value for kw in keywords):
                return {"priority_focus": label}

        offtopic = _offtopic_fallback_check("priority_focus", dispatcher, tracker)
        if offtopic is not None:
            return offtopic

        dispatcher.utter_message(text="Please pick one of the options below, or tell me in your own words.")
        return {"priority_focus": None}


class ActionGenerateTripRecommendations(Action):
    def name(self) -> Text:
        return "action_generate_trip_recommendations"

    def run(
        self, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict[Text, Any]
    ) -> List[EventType]:
        origin_name = tracker.get_slot("origin_city")
        destination_name = tracker.get_slot("destination_city")
        sustainability_level = tracker.get_slot("sustainability_level") or "medium"
        priority_focus = tracker.get_slot("priority_focus") or ""
        budget_currency = tracker.get_slot("budget_currency") or "EUR"

        departure = date_resolver.resolve_date(tracker.get_slot("travel_date_from") or "") or date_cls.today()
        # BUGFIX (same root cause as validate_travel_date_to): a relative
        # return-date phrase ("next week", "the weekend", "next Monday",
        # ...) has to be resolved relative to the DEPARTURE date, not
        # today, or a phrase that was correctly accepted at validation
        # time (resolved against `departure`) could silently re-resolve
        # to a completely different, wrong date here — e.g. producing a
        # bogus/negative night count that only the `nights < 1` safety
        # net below would catch, quietly defaulting to a generic 3
        # nights instead of the date the user actually gave.
        return_text = tracker.get_slot("travel_date_to") or ""
        return_date = date_resolver.resolve_date(return_text, reference=departure)
        nights = date_resolver.nights_between(departure, return_date) if return_date else None
        if (not nights or nights < 1) and return_date:
            # Mirrors validate_travel_date_to's own same-day "weekend"
            # roll-forward (see its comment) — without this, a phrase
            # that was correctly ACCEPTED there (by rolling forward past
            # a same-day departure/return collision) would silently
            # recompute back to the same degenerate 0-night result here
            # and fall straight to the generic 3-night default below,
            # instead of the night count the user actually implied.
            retry_date = date_resolver.resolve_date(return_text, reference=departure + timedelta(days=1))
            retry_nights = date_resolver.nights_between(departure, retry_date) if retry_date else None
            if retry_nights and retry_nights >= 1:
                nights = retry_nights
        if not nights or nights < 1:
            nights = 3  # safety net — validate_travel_date_to should already have prevented this

        try:
            origin_geo = api_clients.geocode(origin_name)
            dest_geo = api_clients.geocode(destination_name)
        except api_clients.ApiError:
            origin_geo = dest_geo = None

        if not origin_geo or not dest_geo:
            dispatcher.utter_message(
                text="Sorry, I lost track of one of those two places — could we start the trip planning again?"
            )
            return reset_fallback_events()

        results: Dict[str, Any] = {
            "origin": origin_name, "destination": destination_name,
            "departure_date": date_resolver.format_date(departure), "nights": nights,
        }

        # ---- independent lookups, fetched CONCURRENTLY --------------------------
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            flight_future = pool.submit(
                _safe_call, api_clients.get_flight_estimate,
                origin_name, origin_geo["lat"], origin_geo["lon"],
                destination_name, dest_geo["lat"], dest_geo["lon"],
            )
            hotels_future = pool.submit(_safe_call, api_clients.find_hotels, dest_geo["lat"], dest_geo["lon"], destination_name)
            transport_future = pool.submit(_safe_call, api_clients.find_transport, dest_geo["lat"], dest_geo["lon"], destination_name)
            attractions_future = pool.submit(_safe_call, api_clients.find_attractions, dest_geo["lat"], dest_geo["lon"], destination_name)
            forecast_future = pool.submit(
                _safe_call, api_clients.get_weather_forecast, dest_geo["lat"], dest_geo["lon"],
                date_resolver.format_date(departure), min(nights, MAX_FORECAST_DAYS_SHOWN),
            )

            flight = flight_future.result()
            hotels_raw = hotels_future.result() or []
            transport_info = transport_future.result() or {"stop_count": None, "summary": "unavailable right now", "source": "error"}
            attractions_raw = attractions_future.result() or []
            forecast = forecast_future.result()

        distance_km = (flight or {}).get("distance_km") or api_clients.haversine_km(
            origin_geo["lat"], origin_geo["lon"], dest_geo["lat"], dest_geo["lon"]
        )

        if flight and budget_currency != flight.get("currency", "EUR"):
            try:
                converted, _rate, _date = api_clients.convert(flight["price"], flight.get("currency", "EUR"), budget_currency)
                flight["price_converted"] = round(converted, 2)
                flight["converted_currency"] = budget_currency
            except api_clients.ApiError:
                pass
        results["flight"] = flight

        # ---- hotels: scored candidates, price scaled by the REAL stay length -----
        hotel_candidates = []
        for h in hotels_raw[:8]:
            stars = h.get("stars")
            try:
                stars_n = float(stars) if stars else 3.0
            except (TypeError, ValueError):
                stars_n = 3.0
            nightly_price = 35 + stars_n * 25
            hotel_candidates.append(
                {
                    "name": h["name"],
                    "eco_certified": bool(h.get("eco_tag")),
                    "is_illustrative": h.get("is_illustrative", False),
                    "stars": stars,
                    "carbon_kg": 15.0,
                    "price": round(nightly_price * nights, 2),
                    "price_is_estimated": True,
                }
            )
        ranked_hotels = scoring.rank_options(hotel_candidates, sustainability_level, priority_focus)
        results["hotels"] = ranked_hotels[:5]

        results["transport_summary"] = transport_info["summary"]
        results["transport_stop_count"] = transport_info.get("stop_count")
        results["attractions"] = attractions_raw[:3]
        results["forecast_days"] = (forecast or {}).get("days", [])

        try:
            mode_comparison = api_clients.compare_modes(distance_km)
        except api_clients.ApiError:
            mode_comparison = []
        results["carbon_by_mode"] = [{"mode": m, "carbon_kg": round(kg, 1)} for m, kg in mode_comparison]
        results["distance_km"] = round(distance_km, 0)

        travel_options = []
        if flight:
            flight_mode = "flight_short" if distance_km < 1500 else "flight_long"
            travel_options.append(
                {
                    "mode": "flight",
                    "price": flight.get("price_converted", flight.get("price")),
                    "carbon_kg": round(api_clients.estimate_carbon_kg(flight_mode, distance_km), 1),
                    "duration_hours": flight.get("duration_hours"),
                }
            )
        # PRACTICALITY GUARD (real user report — Berlin -> "Salta", 11,457 km,
        # scored coach as the best overall option purely on carbon+price
        # dominance, with zero notion of whether a bus or train route is
        # even real). scoring.rank_options is a pure weighted min-max sum of
        # carbon/price with no distance/realism term at all, so the surface-
        # transport fare formula in api_clients.estimate_surface_fare_eur
        # (which has no distance ceiling either) will always produce a
        # cheaper, lower-carbon number than a long-haul flight, no matter
        # how physically absurd the implied journey is (there is, for one
        # thing, no coach or rail route across an ocean).
        #
        # Confirmed against real distances (haversine): Berlin-Potsdam
        # 27 km, Berlin-Prague 281 km, Berlin-Vienna 524 km, Berlin-Munich
        # 504 km, Berlin-Paris 877 km, Berlin-Lisbon 2,312 km, Berlin-Salta
        # 11,457 km. Two separate ceilings, not one, per the user's own
        # split: coach only makes sense for genuinely regional hops
        # (Potsdam/Brandenburg-style trips), while a real intercity RAIL
        # network still comfortably covers medium-distance routes like
        # Berlin-Prague/Vienna/Munich/Paris that a single "short range only"
        # cutoff would have wrongly excluded.
        #   - COACH_MAX_KM = 500: regional/short-hop only (covers Potsdam
        #     and similar; excludes Vienna/Munich and anything farther).
        #   - TRAIN_MAX_KM = 1000 (per explicit user direction — tighter
        #     than the flight_short/flight_long 1500 km split): covers
        #     Prague/Vienna/Munich (281-524 km) and still just covers
        #     London (932 km); a route farther than that (Paris at 877 km
        #     is the last one comfortably inside; Berlin-Rome-scale
        #     ~1180 km and up) gets flight-only, and Lisbon/Salta-scale
        #     distances obviously do too.
        # Beyond both ceilings only "flight" remains in travel_options, so
        # a genuinely long-haul or intercontinental trip is never left with
        # a "cheapest and greenest" surface option that doesn't exist.
        COACH_MAX_KM = 500
        TRAIN_MAX_KM = 1000
        surface_modes = []
        if distance_km <= COACH_MAX_KM:
            surface_modes.append("coach")
        if distance_km <= TRAIN_MAX_KM:
            surface_modes.append("train")
        for mode in surface_modes:
            travel_options.append(
                {
                    "mode": mode,
                    # Distance-based ESTIMATE, not a live quote — no free,
                    # no-key fare API exists for rail/coach (see README
                    # "Data sources"). Previously left as None, which gave
                    # the user nothing to compare against when choosing a
                    # travel mode; see api_clients.estimate_surface_fare_eur
                    # for the formula and its justification.
                    "price": api_clients.estimate_surface_fare_eur(mode, distance_km),
                    "carbon_kg": round(api_clients.estimate_carbon_kg(mode, distance_km), 1),
                    "duration_hours": None,
                }
            )

        priceable = [o for o in travel_options if o["price"] is not None]
        if travel_options and len(priceable) == len(travel_options):
            ranked_travel = scoring.rank_options(travel_options, sustainability_level, priority_focus)
        else:
            ranked_travel = sorted(travel_options, key=lambda o: o["carbon_kg"])
            for idx, option in enumerate(ranked_travel):
                option["tier"] = "green" if idx == 0 else ("amber" if idx == 1 else "red")
        results["travel_options"] = ranked_travel

        events: List[EventType] = [SlotSet("trip_results", results)] + reset_fallback_events()

        dispatcher.utter_message(text=self._summary_text(results, budget_currency))
        dispatcher.utter_message(
            # NOTE: json_message IS the custom payload — CollectingDispatcher
            # sets `"custom": json_message or {}` directly (see
            # rasa_sdk.executor.CollectingDispatcher.utter_message).
            # Wrapping this in an extra {"custom": ...} layer (the old,
            # buggy version) double-nested it, so streamlit_app.py's
            # render_custom() — which reads custom.get("card_type")
            # directly — could never find "card_type" and fell through to
            # a raw JSON dump instead of the trip-summary card. Confirmed
            # against the real rasa_sdk.CollectingDispatcher, not just by
            # reading the code.
            json_message={"card_type": "trip_summary", "data": results}
        )
        # Activates book_confirmation_form (see domain.yml's
        # book_confirmation slot + rules.yml's "Submit book confirmation
        # form" for the full mechanism) — Rasa Core automatically asks
        # the form's own first (and only) required slot right after
        # activation, which is utter_ask_book_confirmation, so it must
        # NOT also be dispatched manually here too (that would show the
        # question twice). Same FollowupAction-to-a-form-name pattern
        # already used by action_route_entry_point elsewhere in this
        # file.
        return events + [FollowupAction("book_confirmation_form")]

    @staticmethod
    def _summary_text(results: Dict[str, Any], currency: str) -> str:
        lines = [f"Here's what I found for **{results['origin']} to {results['destination']}**:"]

        if results.get("travel_options"):
            best = results["travel_options"][0]
            emoji = scoring.tier_emoji(best.get("tier", ""))
            price_txt = (
                f", ~{best['price']:.0f} {currency}"
                if best.get("price") is not None
                else " (fare not available for this mode)"
            )
            lines.append(
                f"{emoji} Best way there: **{best['mode']}** - ~{best['carbon_kg']:.0f} kg CO2e{price_txt}"
            )

        if results.get("hotels"):
            top_hotel = results["hotels"][0]
            emoji = scoring.tier_emoji(top_hotel.get("tier", ""))
            eco_note = " (eco-certified)" if top_hotel.get("eco_certified") else " (eco_certified)"
            lines.append(
                f"{emoji} Top hotel pick: **{top_hotel['name']}** - ~{top_hotel['price']:.0f} {currency} "
                f"for {results['nights']} night{'s' if results['nights'] != 1 else ''}{eco_note}"
            )

        lines.append(f"Public transport near {results['destination']}: {results['transport_summary']}")

        # CHANGED (explicit user request): this short summary message used
        # to print one line PER forecast day (up to MAX_FORECAST_DAYS_SHOWN),
        # which made an already-long message even longer. The full day-by-
        # day breakdown still belongs on the bigger trip_summary card (see
        # ActionGenerateTripRecommendations.run's json_message just above,
        # rendered by streamlit_app.py's render_trip_summary) — this text
        # message now only keeps the one-line general advice for the first
        # forecast day, collapsed onto the same line as the header.
        # forecast_days = results.get("forecast_days") or []
        # if forecast_days:
        #     lines.append(f"Weather during your stay - {forecast_days[0]['advice'].capitalize()}.")

        if results.get("attractions"):
            names = ", ".join(a["name"] for a in results["attractions"])
            lines.append(f"Worth visiting: {names}")

        return "\n".join(lines)


# =========================================================================
# Content drifting, part 1: change_destination_city
# =========================================================================
# User wants a DIFFERENT destination at ANY point in trip_planning_form
# (Q1-Q8) or while book_confirmation_form is asking "Would you like to
# book this?" — see rules.yml's two active_loop-scoped rules that reach
# this action, and domain.yml for the new change_destination_city intent
# (data/nlu.yml has its training examples — deliberately explicit-change
# phrasing only: "actually take me to X instead", "change the destination
# to X", ... — never a bare city name on its own, so this can never be
# confused with a normal first answer to "Where would you like to travel
# to?").

def _destination_change_candidate(tracker: Tracker) -> Optional[Text]:
    """DIET's own trained entity tag on this intent's examples is tried
    FIRST (same division of labour as every city slot in this project:
    DIET first, lenient regex fallback second — see
    city_extractor.extract_destination_change_trigger_lenient's
    docstring)."""
    for entity in (tracker.latest_message or {}).get("entities") or []:
        if entity.get("entity") == "destination_city" and entity.get("value") not in (None, ""):
            return str(entity["value"])
    text = (tracker.latest_message or {}).get("text") or ""
    return city_extractor.extract_destination_change_trigger_lenient(text)


# REMOVED (real-execution root-cause fix, this round): this section used to
# carry _TRIP_PLANNING_SLOT_ORDER / _next_trip_planning_slot /
# _TRIP_PLANNING_REASK_ACTIONS / _TRIP_PLANNING_REASK_RESPONSES /
# _reask_trip_planning_slot — a hand-rolled "figure out the next unfilled
# slot and manually re-utter its question" apparatus, used ONLY by
# ActionHandleDestinationChange's trip_planning_form branch below.
#
# That manual re-ask was the actual bug behind the user's real report
# "after it changed the destination, I gave the answer to the previous
# question and it didn't understand": confirmed via a real trained-model
# run with DEBUG-level policy logs (rasa.shared.core.trackers.set_latest_action)
# that trip_planning_form's rejection flag (tracker.active_loop.rejected —
# set True the moment this content-drift interrupt made the form's own
# slot extraction come up empty, by design, per _is_content_drift_trigger)
# is ONLY ever cleared when the FORM ITSELF is executed again as an action
# (set_latest_action: "reset loop rejection if it was predicted again").
# ActionHandleDestinationChange is a plain custom action, not the form, so
# manually printing the next question here left `rejected` stuck at True
# for every future turn — RulePolicy's default "Predicted loop
# 'trip_planning_form'" auto-continue (rule_policy.py's
# `not active_loop_rejected and ...` check) is suppressed once rejected,
# and since a normal answer like "4" has no dedicated interrupt rule of
# its own, RulePolicy found nothing at all and TEDPolicy's low-confidence
# guess (action_smart_fallback / action_get_carbon_footprint) won instead —
# exactly reproducing the user's transcript.
#
# The fix is rules.yml's "Handle destination change during trip planning"
# rule now ending in `action: trip_planning_form` / `active_loop:
# trip_planning_form` (the SAME proven pattern already used by every
# "Handle off-topic/low-confidence messages during <form>" rule pair in
# this file) instead of stopping at action_handle_destination_change: that
# makes the form run again in the very same turn, which both resets
# `rejected` AND asks the correct next question itself (FormAction.
# request_next_slot recomputes the first still-unfilled required slot from
# scratch every time — confirmed via the same real run: it correctly
# skips destination_city, now filled, and lands on whatever was genuinely
# still pending). That made this hand-rolled duplicate of the form's own
# next-slot logic both wrong and unnecessary, so it's gone; the trip-
# planning branch below now returns nothing but the SlotSet.


class ActionHandleDestinationChange(Action):
    """See the module-level comment above this section for the full
    picture; this class is deliberately thin — almost everything it
    needs already exists elsewhere in this file and is reused, not
    reimplemented.

    DISCLOSED SCOPING DECISION (flagging this rather than silently
    deciding it): a NEW city typed here only gets the SAME curated/live
    exact-match lookup every other city slot uses
    (api_clients.match_city) — but NOT the "Is this what you mean?"
    fuzzy-typo confirmation flow those slots get. That confirmation is
    wired entirely through a form's own extract_<slot>/validate_<slot>
    methods while that exact slot is the form's requested_slot (see
    _awaiting_confirmation_for) — destination_city is never the
    requested_slot at the moment this action runs (some OTHER question,
    or book_confirmation_form's book_confirmation, is), so reusing that
    exact mechanism from here isn't a safe fit without new, dedicated
    pending-state plumbing this round didn't build. A non-exact match is
    treated the same as "not found": the user is asked to retype the
    exact spelling, rather than risk mis-wiring the existing
    confirmation system.
    """

    def name(self) -> Text:
        return "action_handle_destination_change"

    def run(
        self, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict[Text, Any]
    ) -> List[EventType]:
        candidate = _destination_change_candidate(tracker)
        if not candidate:
            dispatcher.utter_message(text="Sure — which city would you like to change the destination to?")
            return []

        match = api_clients.match_city(candidate)
        if not match or not match.get("is_exact"):
            dispatcher.utter_message(
                text=f"I couldn't find an exact match for '{candidate}' — could you type the exact city name?"
            )
            return []

        new_destination = match["matched_name"]
        old_destination = tracker.get_slot("destination_city")

        if old_destination and old_destination.strip().lower() == new_destination.strip().lower():
            dispatcher.utter_message(text=f"You're already headed to {new_destination}.")
            return []

        if old_destination:
            dispatcher.utter_message(text=f"📍 Destination changed from **{old_destination}** to **{new_destination}**.")
        else:
            # Fired at Q1 itself (destination_city not answered yet) —
            # there's nothing to "change" FROM, so this degrades
            # gracefully into just answering Q1, no comparison message.
            dispatcher.utter_message(text=f"📍 Destination set to **{new_destination}**.")

        # Mutating tracker.slots in-memory (NOT persisted by itself — the
        # SlotSet event returned below is what actually persists it) is
        # what lets ActionGenerateTripRecommendations, if reused a few
        # lines down, see the NEW destination when it re-reads
        # tracker.get_slot("destination_city") within this SAME call.
        tracker.slots["destination_city"] = new_destination

        if tracker.get_slot("trip_results") is not None:
            # Past the trip summary already (book_confirmation_form is
            # asking "Would you like to book this?") — per the user's own
            # spec: recompute EVERYTHING for the new destination (same
            # practicality guard, same scoring — it all lives inside this
            # one reused action) and reprint both the summary card and
            # the booking question.
            recompute_events = ActionGenerateTripRecommendations().run(dispatcher, tracker, domain)
            return [SlotSet("destination_city", new_destination)] + recompute_events

        # No manual re-ask here any more (see the removed-helpers comment
        # above) — rules.yml's tail (`action: trip_planning_form` /
        # `active_loop: trip_planning_form` right after this action) makes
        # the form itself run again this same turn and ask whatever is
        # genuinely still the next unfilled required slot.
        return [SlotSet("destination_city", new_destination)]


class ActionDenyBooking(Action):
    """Declining "Would you like to book this?" — per the user's own
    explicit spec, this returns the bot to the initial menu state
    exactly as if the "Clear conversation" button had been pressed,
    rather than showing a closing summary + Offset/Finish buttons (the
    old behaviour). Restarted() wipes the tracker server-side, same as
    ActionClearConversation (actions/fallback.py) — but unlike that
    action, THIS one can be reached by arbitrary typed text ("no need",
    "not now", ...), not only a fixed button payload the frontend can
    recognise in advance, so streamlit_app.py can't special-case the
    OUTGOING payload the way it does for the literal "/clear_conversation"
    button. Instead this sends a small custom marker on its (otherwise
    empty) reply; streamlit_app.py's send_to_rasa() watches for that
    marker on ANY bot reply and performs the exact same local reset
    (wipe session messages, new sender_id) it already does for Clear."""

    def name(self) -> Text:
        return "action_deny_booking"

    def run(
        self, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict[Text, Any]
    ) -> List[EventType]:
        dispatcher.utter_message(json_message={"card_type": "reset_to_menu"})
        return [Restarted()]


class ActionConfirmBooking(Action):
    def name(self) -> Text:
        return "action_confirm_booking"

    def run(
        self, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict[Text, Any]
    ) -> List[EventType]:
        results = tracker.get_slot("trip_results")
        if not results:
            dispatcher.utter_message(
                text="I don't have an active trip to book yet - tell me where you'd like to go first."
            )
            return reset_fallback_events()

        reference = "ECO-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
        # Single merged message (booking line + footprint/attractions +
        # buttons) — this used to be sent as 3 separate bubbles: a plain
        # confirmation line, a "booking_confirmation" custom card (whose
        # only real content was a demo/no-payment disclaimer the user
        # never wanted shown at all), and this footprint+buttons message.
        # The split into 3 wasn't intentional on this project's part —
        # Rasa's own OutputChannel.send_response() always sends a
        # message's `custom` payload as a SEPARATE bubble from its
        # `text`/`buttons`, even when both come from one
        # dispatcher.utter_message() call (confirmed in rasa's own
        # channel.py) — so the fix is to not send a "custom" payload
        # here at all, and fold everything into one text+buttons call.
        lines = [
            f"Booking confirmed - reference **{reference}** for {results['origin']} to {results['destination']}."
        ] + _trip_summary_closing_lines(tracker)
        dispatcher.utter_message(text="\n\n".join(lines), buttons=_TRIP_SUMMARY_BUTTONS)
        return [
            SlotSet("booking_reference", reference),
            SlotSet("awaiting_finish", True),
        ] + reset_fallback_events()


class ValidateBookConfirmationForm(DedupFormValidation):
    """book_confirmation_form — see domain.yml's book_confirmation slot
    comment for the full root cause this fixes. One slot, one job:
    understand a yes/no answer to "Would you like to book this?"
    however it's phrased, exactly as reliably as the rest of this
    project's forms already understand their own free-text answers."""

    def name(self) -> Text:
        return "validate_book_confirmation_form"

    async def extract_book_confirmation(
        self, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict[Text, Any]
    ) -> Dict[Text, Any]:
        if tracker.get_slot("book_confirmation") is not None:
            return {}
        # BUGFIX (real user report + confirmed via real execution, not
        # guessed): book_confirmation_form activates via a FollowupAction
        # from ActionGenerateTripRecommendations, in the SAME turn as
        # the LAST answer to the just-finished trip_planning_form (e.g.
        # "balanced" for priority_focus). On that very first pass,
        # tracker.latest_message is still THAT stale message — nothing
        # new has been said yet, since this form hasn't even asked its
        # own question. Without this guard, "balanced" got treated as a
        # failed attempt to answer "Would you like to book this?",
        # printing "Sorry, that doesn't seem to answer what I just
        # asked" BEFORE the real question ever appeared. Confirmed via a
        # real trained-model run that at the moment this extractor is
        # invoked on that stray pass, requested_slot is still whatever
        # it was BEFORE this form activated (None, or the previous
        # form's last slot) — never yet "book_confirmation" — so gating
        # on that is precise and costs nothing: this form has exactly
        # one slot, so there's no OTHER slot of its own worth extracting
        # opportunistically the way weather_check_form's two slots are
        # (see extract_weather_city/extract_weather_day_text above,
        # which stay unconditional deliberately, for that reason).
        if tracker.get_slot("requested_slot") != "book_confirmation":
            return {}
        # See _is_content_drift_trigger's docstring — same `type: custom`
        # bypass-of-not_intent fix as extract_origin_city above: without
        # this, "actually, change the destination to Vienna" sent while
        # "Would you like to book this?" is pending would be swallowed
        # as a (bogus) yes/no answer instead of leaving this slot empty
        # so the form rejects and action_handle_destination_change gets
        # a chance to run (see rules.yml's "... during book confirmation"
        # rule).
        if _is_content_drift_trigger(tracker, "change_destination_city"):
            return {}
        text = (tracker.latest_message or {}).get("text") or ""
        # Unlike the city-slot extractors, a "/"-prefixed payload here
        # ("/confirm_booking", "/deny_booking" — the two buttons' own
        # payloads) is exactly the expected input, not something to
        # filter out: validate_book_confirmation below decides yes/no
        # from the parsed INTENT for these, not from keyword-matching
        # the literal payload text. Their own intents (confirm_booking/
        # deny_booking) are never one of the 6 foreign entry intents
        # anyway, so _foreign_switch_unless_booking_answer below is a
        # no-op for them regardless.
        candidate = text.strip()
        if not candidate:
            return {}
        # See _foreign_switch_unless_booking_answer's docstring — same
        # idea as the city/date extractors' guards: without this,
        # "actually let's check the weather instead" typed while "Would
        # you like to book this?" is pending would be swallowed as a
        # (bogus) yes/no answer instead of leaving this slot empty so
        # the form rejects and action_handle_task_switch gets a chance
        # to run — but ONLY once this text has already failed to look
        # like a real yes/no answer, so a genuine (if misclassified)
        # answer is still accepted. No own_intents here —
        # book_confirmation_form is never itself entered via one of the
        # 6 entry-point intents (it's only ever reached via
        # ActionGenerateTripRecommendations's own FollowupAction), so
        # every one of them is a genuinely foreign switch away from it.
        if _foreign_switch_unless_booking_answer(tracker, candidate):
            return {}
        return {"book_confirmation": candidate}

    def validate_book_confirmation(
        self, slot_value: Any, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict[Text, Any]
    ) -> Dict[Text, Any]:
        text = str(slot_value).strip()
        intent = (tracker.latest_message or {}).get("intent", {}).get("name")

        if intent == "confirm_booking" or _looks_like_booking_yes(text):
            return {"book_confirmation": "yes"}
        if intent == "deny_booking" or _looks_like_booking_no(text):
            return {"book_confirmation": "no"}

        offtopic = _offtopic_fallback_check("book_confirmation", dispatcher, tracker)
        if offtopic is not None:
            return offtopic

        dispatcher.utter_message(text="Sorry, I didn't quite catch that — would you like to book it, yes or no?")
        return {"book_confirmation": None}


class ActionRouteBookConfirmation(Action):
    """Runs right after book_confirmation_form's single slot is filled —
    reads the yes/no decision validate_book_confirmation already worked
    out and hands off to the SAME action_confirm_booking/
    action_deny_booking logic that existed before this form did.

    IMPORTANT: this calls ActionConfirmBooking()/ActionDenyBooking()'s
    run() method DIRECTLY (as plain Python), rather than returning a
    FollowupAction("action_confirm_booking"/"action_deny_booking") the
    way action_route_entry_point routes to a FORM. Confirmed via real
    execution that the FollowupAction version is broken: RulePolicy
    only ever "sees" the literal action steps written in rules.yml, so
    the transition previous_action=action_route_book_confirmation ->
    action_confirm_booking (a runtime-only FollowupAction) was never a
    trained state, and neither was action_confirm_booking -> action_listen
    afterwards. With nothing predicting a next action confidently,
    config.yml's core_fallback_action_name kicked in and silently ran
    action_smart_fallback right after "Booking confirmed" — printing the
    exact generic 4-button menu this whole change was meant to stop.
    action_route_entry_point's own FollowupAction pattern doesn't have
    this problem because its targets are always FORMS: a form's own
    active_loop condition makes the "Submit ... form" rule match
    regardless of how the loop became active, so RulePolicy always knows
    what happens next. action_confirm_booking/action_deny_booking are
    plain actions, not loops, so that safety net doesn't apply to them.
    Calling their run() methods inline instead means Rasa Core only ever
    sees ONE action executed here (action_route_book_confirmation) — and
    the transition from THAT back to action_listen is exactly the
    already-proven-working pattern "Submit trip planning form" uses for
    action_generate_trip_recommendations (a regular action as a rule's
    own literal last step, not a runtime FollowupAction target)."""

    def name(self) -> Text:
        return "action_route_book_confirmation"

    def run(
        self, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict[Text, Any]
    ) -> List[EventType]:
        decision = tracker.get_slot("book_confirmation")
        target = ActionConfirmBooking() if decision == "yes" else ActionDenyBooking()
        return [SlotSet("book_confirmation", None)] + target.run(dispatcher, tracker, domain)


_TRIP_SUMMARY_BUTTONS = [
    {"title": "🌱 Offset", "payload": "/ask_carbon_offset_info"},
    # BUGFIX (real user request): label kept in sync with
    # utter_finish_button/utter_awaiting_finish_reminder in domain.yml —
    # this is the SAME Finish button (same "/finish" payload), just
    # shown here without the "Is there anything else…" message (this
    # one follows the booking-confirmation trip summary text instead).
    {"title": "Finish / main menu", "payload": "/finish"},
]


def _trip_summary_closing_lines(tracker: Tracker) -> List[Text]:
    """The closing carbon-footprint recap + cultural-attractions blurb
    shown after either a booking confirmation or a decline — shared by
    ActionConfirmBooking and ActionDenyBooking since this content is
    about the TRIP, not the booking decision itself. Returns plain
    lines for the CALLER to merge into its own single message (see
    those two classes) rather than sending a message of its own, so the
    whole thing renders as one chat bubble with one set of buttons."""
    results = tracker.get_slot("trip_results") or {}

    lines = []
    if results.get("carbon_by_mode"):
        best_mode = min(results["carbon_by_mode"], key=lambda m: m["carbon_kg"])
        lines.append(
            f"Estimated footprint for this trip: as low as ~{best_mode['carbon_kg']:.0f} kg CO2e "
            f"by {best_mode['mode']}, over ~{results.get('distance_km', 0):.0f} km."
        )
    attractions = results.get("attractions") or []
    if attractions:
        names = ", ".join(a["name"] for a in attractions)
        lines.append(f"While you're there, {names} are well worth building into your itinerary.")

    if not lines:
        lines.append("Have a wonderful trip!")

    return lines


class ActionOffsetAndFinish(Action):
    def name(self) -> Text:
        return "action_offset_and_finish"

    def run(
        self, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict[Text, Any]
    ) -> List[EventType]:
        dispatcher.utter_message(response="utter_carbon_offset_info")
        dispatcher.utter_message(response="utter_finish_button")
        return [SlotSet("awaiting_finish", True)]


class ActionSeeMoreOptions(Action):
    def name(self) -> Text:
        return "action_see_more_options"

    def run(
        self, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict[Text, Any]
    ) -> List[EventType]:
        results = tracker.get_slot("trip_results")
        if not results or not results.get("hotels"):
            dispatcher.utter_message(text="I don't have more options stored right now - let's plan a trip first.")
            return reset_fallback_events()

        currency = tracker.get_slot("budget_currency") or "EUR"
        lines = ["Here are a few more hotel options:"]
        for h in results["hotels"]:
            emoji = scoring.tier_emoji(h.get("tier", ""))
            lines.append(f"{emoji} {h['name']} - ~{h['price']:.0f} {currency} for {results.get('nights', 3)} nights")

        dispatcher.utter_message(
            text="\n".join(lines),
            # See the note in ActionGenerateTripRecommendations.run() above —
            # json_message IS the custom payload, no extra "custom" wrapper.
            json_message={"card_type": "more_hotels", "data": results["hotels"]},
        )
        return reset_fallback_events()


class ActionGetCarbonFootprint(Action):
    def name(self) -> Text:
        return "action_get_carbon_footprint"

    def run(
        self, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict[Text, Any]
    ) -> List[EventType]:
        results = tracker.get_slot("trip_results")
        if not results or not results.get("carbon_by_mode"):
            dispatcher.utter_message(
                text=(
                    "I don't have a trip to compare yet - tell me where you're travelling "
                    "from and to, and I'll break the carbon footprint down by transport mode."
                )
            )
            return reset_fallback_events()

        lines = [f"Estimated CO2e for {results['origin']} to {results['destination']} (~{results['distance_km']:.0f} km) by mode:"]
        for m in results["carbon_by_mode"]:
            lines.append(f"  - {m['mode']}: ~{m['carbon_kg']:.0f} kg CO2e")
        dispatcher.utter_message(text="\n".join(lines))
        return reset_fallback_events()


# =========================================================================
# Weather Checking - weather_check_form
# =========================================================================

class ValidateWeatherCheckForm(DedupFormValidation):
    def name(self) -> Text:
        return "validate_weather_check_form"

    async def extract_weather_city(
        self, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict[Text, Any]
    ) -> Dict[Text, Any]:
        if tracker.get_slot("weather_city") is not None:
            return {}
        text = (tracker.latest_message or {}).get("text") or ""
        if text.startswith("/"):
            # See extract_destination_city's comment (ValidateTripPlanningForm,
            # above) — same fix, same real bug, confirmed via a real
            # debug log this time: clicking "Yes" to confirm a
            # fuzzy-matched weather_city suggestion logged "Execution of
            # 'weather_check_form' was rejected. Setting its confidence
            # to 0.0 in all predictions." and fell through to
            # action_smart_fallback, instead of resolving to the
            # suggested city — because this early return blocked
            # weather_city from ever being extracted on that turn at
            # all (its domain.yml mapping is `type: custom`, so there's
            # no other extraction path), meaning validate_weather_city
            # (where _match_city_or_ask's "Yes" resolution actually
            # lives) was never even called.
            if _awaiting_confirmation_for(tracker, "weather_city"):
                return {"weather_city": text.strip()}
            # See extract_destination_city's comment (ValidateTripPlanningForm)
            # — same fix, applied uniformly. weather_city has no direct
            # quick-pick button of its own today, but this guards
            # against the same rejection if one is ever added, exactly
            # like _awaiting_confirmation_for was already applied here.
            structured_value = _structured_payload_value(tracker, "weather_city")
            if structured_value is not None:
                return {"weather_city": structured_value}
            return {}
        # Lenient "in X" / "for X" / "at X" / "of X" / possessive "X's
        # weather" match (see city_extractor's module docstring).
        #
        # BUGFIX (real user report): "check the weather of Tehran" sent
        # as the FIRST message — which both triggers ask_weather AND
        # activates weather_check_form in the same turn — used to lose
        # "Tehran" entirely, because this only ran once weather_city was
        # ALREADY the requested_slot, i.e. only on a SEPARATE later
        # turn. Unconditional now — safe because it requires an
        # explicit trigger word ("of"/"in"/"for"/"at"/possessive "'s"),
        # confirmed via city_extractor's own docstring reasoning (this
        # is the SAME fix applied to destination/origin_city above, and
        # unlike book_confirmation's extract method, weather_check_form
        # is only ever entered via THIS SAME triggering message — never
        # via a cross-form FollowupAction reusing a stale, unrelated
        # message — so there's no activation-time "wrong message" risk
        # here to guard against).
        lenient_match = city_extractor.extract_city_trigger_lenient(text)
        if lenient_match:
            return {"weather_city": lenient_match}
        if tracker.get_slot("requested_slot") == "weather_city":
            candidate = text.strip()
            if not candidate:
                return {}
            # See _foreign_switch_unless_real_city's docstring — this
            # slot's `type: custom` mapping bypasses domain.yml's
            # `not_intent` entirely, so a foreign task-switch message
            # ("actually, let's plan a trip instead") must be caught
            # here too, but ONLY once a real city lookup on the raw text
            # has already failed — confirmed via real execution that a
            # plain, genuinely valid city name ("Wurzburg") typed to
            # answer this exact question can be misclassified by DIET as
            # e.g. ask_trip_planning and must still be accepted.
            # own_intents includes ask_weather itself so this never
            # blocks the very message that activates this form (that
            # triggering message's own intent IS ask_weather).
            if _foreign_switch_unless_real_city(tracker, candidate, own_intents=frozenset({"ask_weather"})):
                return {}
            return {"weather_city": candidate}
        return {}

    async def extract_weather_day_text(
        self, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict[Text, Any]
    ) -> Dict[Text, Any]:
        if tracker.get_slot("weather_day_text") is not None:
            return {}
        # See _is_content_drift_trigger's docstring — this slot's `type:
        # custom` mapping bypasses domain.yml's `not_intent` entirely, so
        # without this, "I want to go there from Berlin" sent mid weather
        # check (weather_city already answered, this slot still pending)
        # would fall through to date_resolver/the bare requested_slot
        # fallback below and get swallowed as a (bogus, unparseable)
        # date candidate, instead of leaving this slot empty so the form
        # rejects and action_start_trip_from_weather_drift gets a chance
        # to run (see rules.yml's "Drift from weather checking to trip
        # planning, mid-form" rule). Confirmed via a real trained-model
        # run: without this guard, DIET classified the drift intent at
        # 99.98% confidence but the message was still absorbed here as a
        # failed day-of-week answer.
        if _is_content_drift_trigger(tracker, "ask_trip_to_this_city"):
            return {}
        text = (tracker.latest_message or {}).get("text") or ""
        if text.startswith("/"):
            # BUGFIX (real user report + real debug log: clicking the
            # "Tomorrow" — and every other — day-suggestion button
            # after picking a city produced "Sorry, that doesn't seem
            # to answer what I just asked" instead of resolving the
            # day, even though typing "tomorrow" by hand worked fine).
            # See _structured_payload_value's docstring for the full
            # root cause: this slot's 6 quick-reply buttons (domain.yml's
            # utter_ask_weather_day_text) all send a structured
            # `/inform_dates{"weather_day_text": "..."}` payload, which
            # is the ONLY way those buttons can ever answer this
            # question — but this early return was blocking it
            # unconditionally, and weather_day_text's `type: custom`
            # mapping means there's no other extraction path to pick
            # it up instead.
            structured_value = _structured_payload_value(tracker, "weather_day_text")
            if structured_value is not None:
                return {"weather_day_text": structured_value}
            return {}
        # BUGFIX (real user report): "what is weather of Tehran at the
        # weekend?" / "what is Berlin's weather today?" — the day
        # mention in the SAME message as the city was silently thrown
        # away, and the bot asked "Which day...?" from scratch even
        # though the user had already said it. Safe to try
        # unconditionally (not gated to requested_slot=="weather_day_text")
        # because date_resolver.resolve_date only recognises a narrow,
        # specific set of real date/relative-day phrases (see that
        # module's own docstring) — it can't accidentally misinterpret
        # an unrelated answer as a date the way a bare "assume the whole
        # message" guess could, so there's no false-positive risk in
        # running it on every message.
        if date_resolver.resolve_date(text) is not None:
            return {"weather_day_text": text.strip()}
        if tracker.get_slot("requested_slot") == "weather_day_text":
            candidate = text.strip()
            if not candidate:
                return {}
            # See _foreign_switch_unless_real_date's docstring — a bare
            # fallback answer here must not be bounced away as a task
            # switch unless it ALSO genuinely fails to resolve as a
            # date (mirrors the Wurzburg/ask_trip_planning false-positive
            # fix applied to extract_weather_city above: a real day
            # answer that DIET happens to misclassify as a foreign
            # entry intent must still be accepted).
            if _foreign_switch_unless_real_date(tracker, candidate, own_intents=frozenset({"ask_weather"})):
                return {}
            return {"weather_day_text": candidate}
        return {}

    def validate_weather_city(
        self, slot_value: Any, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict[Text, Any]
    ) -> Dict[Text, Any]:
        return _match_city_or_ask("weather_city", str(slot_value), dispatcher, tracker)

    def validate_weather_day_text(
        self, slot_value: Any, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict[Text, Any]
    ) -> Dict[Text, Any]:
        # ORDER — see validate_travel_date_from's comment. Parse first.
        text = str(slot_value).strip()
        if text and len(text) <= 100 and date_resolver.resolve_date(text) is not None:
            return {"weather_day_text": text}

        offtopic = _offtopic_fallback_check("weather_day_text", dispatcher, tracker)
        if offtopic is not None:
            return offtopic

        if not text or len(text) > 100:
            dispatcher.utter_message(text="Sorry, could you give me a day, e.g. a date or \"next Sunday\"?")
        else:
            dispatcher.utter_message(
                text=f"I couldn't work out a date from '{text}' - try something like \"tomorrow\" or \"2026-12-16\"."
            )
        return {"weather_day_text": None}


class ActionAnswerWeatherCheck(Action):
    def name(self) -> Text:
        return "action_answer_weather_check"

    def run(
        self, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict[Text, Any]
    ) -> List[EventType]:
        city = tracker.get_slot("weather_city")
        day_text = tracker.get_slot("weather_day_text")
        resolved_date = date_resolver.resolve_date(day_text or "") or date_cls.today()
        # `last_checked_city` (content-drifting feature): weather_city
        # itself gets reset to None below (always has, so a later, unrelated
        # weather check never starts pre-filled with a stale city) — but
        # ActionStartTripFromWeatherDrift needs to know "this city" after
        # the Finish button has already been shown, i.e. AFTER weather_city
        # is wiped. Mirrored here, at the one place weather_city's real
        # value is about to disappear, rather than skipping the reset
        # (which would reintroduce the exact carry_over_slots_to_new_session
        # contamination this reset already guards against). Only ever read
        # by that one action, and only in direct response to an explicit
        # "take me there" style message — never surfaced on its own.
        # ActionAnswerHotelList writes the same slot, so the most recent of
        # the two checks wins.
        reset_events = [
            SlotSet("weather_city", None),
            SlotSet("weather_day_text", None),
            SlotSet("last_checked_city", city),
        ]

        try:
            geo = api_clients.geocode(city)
        except api_clients.ApiError:
            geo = None
        if not geo:
            dispatcher.utter_message(text=f"Sorry, I couldn't find a place called {city}.")
            return reset_fallback_events() + reset_events

        forecast = api_clients.get_weather_forecast(geo["lat"], geo["lon"], date_resolver.format_date(resolved_date), 1)
        if not forecast or not forecast.get("days"):
            dispatcher.utter_message(text="Sorry, the weather service isn't responding right now - please try again shortly.")
            return reset_fallback_events() + reset_events

        day = forecast["days"][0]
        note = " (seasonal estimate, not a live forecast)" if day.get("source") == "seasonal_estimate" else ""
        dispatcher.utter_message(
            text=(
                f"Weather in {city} on {day['date']}: ~{day['temperature_max_c']:.0f}C, "
                f"{day['precipitation_mm']:.0f} mm rain{note}. {day['advice'].capitalize()}."
            )
        )
        dispatcher.utter_message(response="utter_finish_button")
        return reset_fallback_events() + reset_events + [SlotSet("awaiting_finish", True)]


# =========================================================================
# Content drifting, part 2: ask_trip_to_this_city
# =========================================================================
# User wants to plan a trip to the same city they were just checking the
# weather for or just listed hotels for — either mid weather_check_form
# (weather_city already answered, weather_day_text still pending: see
# rules.yml's "active_loop: weather_check_form"-scoped rule), or any time
# after either flow finished, including after the Finish button already
# printed (weather_city / hotel_list_city themselves are None by then — see
# the last_checked_city mirror in ActionAnswerWeatherCheck and
# ActionAnswerHotelList; see rules.yml's OTHER, unconditioned rule for this
# same intent).

# Words that point at a place without naming one ("make a trip to there",
# "plan a trip to that city"). The lenient "to X" extractor returns them as
# if they were a city, so they must never be taken as a destination.
_PLACE_PRONOUNS = frozenset({
    "there", "here", "that place", "this place", "that city", "this city",
    "that location", "this location", "that destination", "this destination",
})


def _named_destination(text: Text) -> Optional[Text]:
    """A real city named as the destination inside the message itself
    ("... from Berlin to Paris ..."). If the user names one, the message is
    a normal trip request, whatever intent NLU gave it, and "there" has
    nothing to refer to."""
    candidate = city_extractor.extract_destination_trigger(text)
    if candidate:
        return candidate
    candidate = city_extractor.extract_destination_trigger_lenient(text)
    if candidate and candidate.strip().lower() not in _PLACE_PRONOUNS:
        match = api_clients.match_city(candidate)
        if match and match.get("found"):
            return candidate
    return None


def _weather_drift_origin_candidate(tracker: Tracker) -> Optional[Text]:
    """Same DIET-entity-first, lenient-regex-second pattern as every
    other city slot — an origin city given INLINE in the same drift
    message ("I want to go there from Berlin") lets the new
    trip_planning_form skip straight past Q2 too, not just Q1."""
    for entity in (tracker.latest_message or {}).get("entities") or []:
        if entity.get("entity") == "origin_city" and entity.get("value") not in (None, ""):
            return str(entity["value"])
    text = (tracker.latest_message or {}).get("text") or ""
    return city_extractor.extract_origin_trigger_lenient(text)


class ActionStartTripFromWeatherDrift(Action):
    """Handles "plan a trip there" after a weather check OR a hotel list
    (the action keeps its original name; the city can now come from either
    flow — see ActionAnswerWeatherCheck / ActionAnswerHotelList, which both
    mirror the city they just answered into last_checked_city).

    Deliberately does NOT check awaiting_finish anywhere in this
    method — per the user's own explicit spec, this is the ONE
    exception to "everything after the Finish message is fallback-
    triggering". A real trained intent (ask_trip_to_this_city) reaching
    this action at all already means DIET was confident this is what the
    user meant, Finish message showing or not.
    """

    def name(self) -> Text:
        return "action_start_trip_from_weather_drift"

    def run(
        self, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict[Text, Any]
    ) -> List[EventType]:
        text = (tracker.latest_message or {}).get("text") or ""
        named = _named_destination(text)
        # A city named in the message wins over "there". Otherwise an active
        # weather_city (weather_check_form still open, day not answered yet)
        # is the city being discussed right now; failing that, the most
        # recent city from either finished flow.
        city = None if named else (tracker.get_slot("weather_city") or tracker.get_slot("last_checked_city"))
        if not city:
            # Nothing to carry over: hand over to trip_planning_form, which
            # reads the destination/origin/dates already in this message, or
            # asks Q1 itself and accepts the answer whatever NLU makes of a
            # bare city name. awaiting_finish is cleared so the Finish
            # reminder can't block it.
            dispatcher.utter_message(text="Sure — let's plan a trip! 🌍")
            return [
                ActiveLoop(None),
                SlotSet("weather_city", None),
                SlotSet("weather_day_text", None),
                SlotSet("awaiting_finish", False),
                FollowupAction("trip_planning_form"),
            ] + reset_fallback_events()

        events: List[EventType] = [
            ActiveLoop(None),  # formally exits weather_check_form's loop, if it's still active
            SlotSet("weather_city", None),
            SlotSet("weather_day_text", None),
            SlotSet("awaiting_finish", False),
            SlotSet("destination_city", city),
        ] + reset_fallback_events()

        origin_candidate = _weather_drift_origin_candidate(tracker)
        if origin_candidate:
            events.append(SlotSet("origin_city", origin_candidate))

        dispatcher.utter_message(text=f"Great — let's plan a trip to **{city}**! 🌍")
        # trip_planning_form asks the FIRST still-empty required slot in
        # its own required_slots order (see domain.yml) — destination_city
        # is already filled above, so it naturally resumes at Q2
        # (origin_city), or Q3 (travel_date_from) if origin was ALSO
        # given inline this same turn. No special-case "resume at Q2/Q3"
        # logic needed here at all; this is just how Rasa forms already
        # work.
        events.append(FollowupAction("trip_planning_form"))
        return events


# =========================================================================
# Currency Exchange - currency_exchange_form
# =========================================================================

_CURRENCY_NAME_TO_CODE = {
    "euro": "EUR", "euros": "EUR", "dollar": "USD", "dollars": "USD", "usd": "USD",
    "pound": "GBP", "pounds": "GBP", "sterling": "GBP", "yen": "JPY",
    "swiss franc": "CHF", "franc": "CHF", "yuan": "CNY", "renminbi": "CNY",
}
_CURRENCY_PAIR_PATTERN = re.compile(
    r"(?i:\b(euro|euros|dollar|dollars|pound|pounds|yen|franc|swiss franc|[a-z]{3})\b\s+(?:to|->|/|-)\s+"
    r"\b(euro|euros|dollar|dollars|pound|pounds|yen|franc|swiss franc|[a-z]{3})\b)"
)


def _normalise_currency(text: str) -> Optional[str]:
    t = text.strip().lower()
    if t in _CURRENCY_NAME_TO_CODE:
        return _CURRENCY_NAME_TO_CODE[t]
    if re.fullmatch(r"[a-zA-Z]{3}", t):
        return t.upper()
    return None


class ValidateCurrencyExchangeForm(DedupFormValidation):
    def name(self) -> Text:
        return "validate_currency_exchange_form"

    async def extract_from_currency(
        self, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict[Text, Any]
    ) -> Dict[Text, Any]:
        if tracker.get_slot("from_currency") is not None:
            return {}
        text = (tracker.latest_message or {}).get("text") or ""
        if text.startswith("/"):
            return {}
        m = _CURRENCY_PAIR_PATTERN.search(text)
        if m:
            from_c, to_c = _normalise_currency(m.group(1)), _normalise_currency(m.group(2))
            if from_c and to_c:
                return {"from_currency": from_c, "to_currency": to_c}
        if tracker.get_slot("requested_slot") == "from_currency":
            candidate = _normalise_currency(text)
            if candidate:
                return {"from_currency": candidate}
        return {}

    def validate_from_currency(
        self, slot_value: Any, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict[Text, Any]
    ) -> Dict[Text, Any]:
        # ORDER — see validate_travel_date_from's comment. Parse first.
        code = _normalise_currency(str(slot_value))
        if code:
            return {"from_currency": code}

        offtopic = _offtopic_fallback_check("from_currency", dispatcher, tracker)
        if offtopic is not None:
            return offtopic

        dispatcher.utter_message(text='Please give me a currency code or name, e.g. "EUR" or "euro".')
        return {"from_currency": None}

    def validate_to_currency(
        self, slot_value: Any, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict[Text, Any]
    ) -> Dict[Text, Any]:
        # ORDER — see validate_travel_date_from's comment. Parse first.
        code = _normalise_currency(str(slot_value))
        if code:
            return {"to_currency": code}

        offtopic = _offtopic_fallback_check("to_currency", dispatcher, tracker)
        if offtopic is not None:
            return offtopic

        dispatcher.utter_message(text='Please give me a currency code or name, e.g. "USD" or "dollar".')
        return {"to_currency": None}

    _NUMBER_PATTERN = re.compile(r"\d[\d,]*\.?\d*")

    def validate_exchange_amount(
        self, slot_value: Any, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict[Text, Any]
    ) -> Dict[Text, Any]:
        # ORDER — see validate_travel_date_from's comment. Parse first.
        numbers = self._NUMBER_PATTERN.findall(str(slot_value))
        if numbers:
            amount = float(numbers[0].replace(",", ""))
            if amount > 0:
                return {"exchange_amount": amount}
            dispatcher.utter_message(text="I need a positive amount.")
            return {"exchange_amount": None}

        offtopic = _offtopic_fallback_check("exchange_amount", dispatcher, tracker)
        if offtopic is not None:
            return offtopic

        dispatcher.utter_message(text='I need a number - e.g. "500".')
        return {"exchange_amount": None}


class ActionAnswerCurrencyExchange(Action):
    def name(self) -> Text:
        return "action_answer_currency_exchange"

    def run(
        self, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict[Text, Any]
    ) -> List[EventType]:
        from_c = tracker.get_slot("from_currency")
        to_c = tracker.get_slot("to_currency")
        amount = tracker.get_slot("exchange_amount") or 1
        reset_events = [SlotSet("from_currency", None), SlotSet("to_currency", None), SlotSet("exchange_amount", None)]

        try:
            converted, _rate, _date = api_clients.convert(amount, from_c, to_c)
            dispatcher.utter_message(text=f"{amount:.0f} {from_c} = {converted:.2f} {to_c}")
        except api_clients.ApiError:
            dispatcher.utter_message(text="Sorry, the currency service isn't responding right now - please try again shortly.")
            return reset_fallback_events() + reset_events

        dispatcher.utter_message(response="utter_finish_button")
        return reset_fallback_events() + reset_events + [SlotSet("awaiting_finish", True)]


# =========================================================================
# Human Advisor - human_advisor_form
# =========================================================================

class ValidateHumanAdvisorForm(DedupFormValidation):
    def name(self) -> Text:
        return "validate_human_advisor_form"

    def validate_contact_info(
        self, slot_value: Any, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict[Text, Any]
    ) -> Dict[Text, Any]:
        offtopic = _offtopic_fallback_check("contact_info", dispatcher, tracker)
        if offtopic is not None:
            return offtopic
        text = str(slot_value).strip()
        if not text or len(text) > 200:
            dispatcher.utter_message(text="Could you share a valid email address or phone number?")
            return {"contact_info": None}
        return {"contact_info": text}


class ActionSubmitHumanAdvisor(Action):
    def name(self) -> Text:
        return "action_submit_human_advisor"

    def run(
        self, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict[Text, Any]
    ) -> List[EventType]:
        contact = tracker.get_slot("contact_info")
        reference = "ADV-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
        send_to_human_queue(
            {
                "handover_reference": reference,
                "conversation_id": tracker.sender_id,
                "contact_info": contact,
                "collected_slots": {k: tracker.get_slot(k) for k in ("origin_city", "destination_city") if tracker.get_slot(k)},
                "request_type": "proactive_human_advisor",
            }
        )
        dispatcher.utter_message(
            text=f"Thanks - reference **{reference}**. One of our specialists will reach out to you at {contact} as soon as possible."
        )
        dispatcher.utter_message(response="utter_finish_button")
        return [SlotSet("contact_info", None), SlotSet("awaiting_finish", True)] + reset_fallback_events()


# NOTE: action_handover_to_human is intentionally NOT redefined here — the
# original, more complete implementation (packages last_intent/confidence
# too, via _build_transcript/_collected_slots) already lives in
# fallback.py and is reused as-is. It's a genuinely different flow from
# ActionSubmitHumanAdvisor above (mid-conversation escalation with a
# transcript vs. proactively asking for contact info), so both exist
# side by side rather than one replacing the other.


# =========================================================================
# Trip Assistant sub-buttons: "cities list" (one-shot) and "hotel list"
# (its own tiny form, since it needs a city first)
# =========================================================================

class ActionShowCitiesList(Action):
    def name(self) -> Text:
        return "action_show_cities_list"

    def run(
        self, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict[Text, Any]
    ) -> List[EventType]:
        try:
            grouped = api_clients.list_cities()
        except api_clients.ApiError:
            grouped = {}

        if not grouped:
            dispatcher.utter_message(text="Sorry, the cities list isn't available right now — please try again shortly.")
            dispatcher.utter_message(response="utter_finish_button")
            return [SlotSet("awaiting_finish", True)] + reset_fallback_events()

        lines = ["Here are the destinations I have data for:"]
        for continent in ("Europe", "Middle East", "Asia", "Africa", "North America", "South America", "Oceania"):
            cities = grouped.get(continent)
            if cities:
                lines.append(f"**{continent}:** {', '.join(cities)}")
        dispatcher.utter_message(text="\n\n".join(lines))
        dispatcher.utter_message(response="utter_finish_button")
        return [SlotSet("awaiting_finish", True)] + reset_fallback_events()


class ValidateHotelListForm(DedupFormValidation):
    def name(self) -> Text:
        return "validate_hotel_list_form"

    async def extract_hotel_list_city(
        self, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict[Text, Any]
    ) -> Dict[Text, Any]:
        if tracker.get_slot("hotel_list_city") is not None:
            return {}
        text = (tracker.latest_message or {}).get("text") or ""
        if text.startswith("/"):
            # See extract_destination_city's comment (ValidateTripPlanningForm)
            # — same fix, same real bug (a pending "Yes" confirmation for
            # THIS slot must still reach validate_hotel_list_city, or the
            # form gets rejected by Core instead of resolving the
            # suggested city).
            if _awaiting_confirmation_for(tracker, "hotel_list_city"):
                return {"hotel_list_city": text.strip()}
            # See extract_destination_city's comment (ValidateTripPlanningForm)
            # — same fix, applied uniformly (no direct quick-pick button
            # exists for this slot today, but this guards against the
            # same rejection if one is ever added).
            structured_value = _structured_payload_value(tracker, "hotel_list_city")
            if structured_value is not None:
                return {"hotel_list_city": structured_value}
            return {}
        # See extract_weather_city's comment above — same lenient
        # "in X" / "for X" / "at X" / "of X" fallback, also
        # unconditional now for the same reason and by the same
        # uniform-fix reasoning (hotel_list_form is likewise only ever
        # entered via its own triggering message, never a cross-form
        # stale-message handoff).
        lenient_match = city_extractor.extract_city_trigger_lenient(text)
        if lenient_match:
            return {"hotel_list_city": lenient_match}
        if tracker.get_slot("requested_slot") == "hotel_list_city":
            candidate = text.strip()
            if not candidate:
                return {}
            # See _foreign_switch_unless_real_city's docstring — this
            # slot's `type: custom` mapping bypasses domain.yml's
            # `not_intent` entirely, so a foreign task-switch message
            # must be caught here too, but ONLY once a real city lookup
            # on the raw text has already failed (same Wurzburg-style
            # false-positive fix applied to extract_weather_city above).
            # own_intents includes see_hotel_list itself so this never
            # blocks the very message that activates this form.
            if _foreign_switch_unless_real_city(tracker, candidate, own_intents=frozenset({"see_hotel_list"})):
                return {}
            return {"hotel_list_city": candidate}
        return {}

    def validate_hotel_list_city(
        self, slot_value: Any, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict[Text, Any]
    ) -> Dict[Text, Any]:
        return _match_city_or_ask("hotel_list_city", str(slot_value), dispatcher, tracker)


class ActionAnswerHotelList(Action):
    def name(self) -> Text:
        return "action_answer_hotel_list"

    def run(
        self, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict[Text, Any]
    ) -> List[EventType]:
        city = tracker.get_slot("hotel_list_city")
        try:
            geo = api_clients.geocode(city)
        except api_clients.ApiError:
            geo = None
        if not geo:
            dispatcher.utter_message(text=f"Sorry, I couldn't find a place called {city}.")
            return reset_fallback_events() + [SlotSet("hotel_list_city", None)]

        # Same mirror as ActionAnswerWeatherCheck: hotel_list_city is reset
        # below, but "make a trip to there" typed after the Finish button
        # must still know which city this was (ActionStartTripFromWeatherDrift
        # reads last_checked_city). Only set once the city is known to exist.
        hotels = api_clients.find_hotels(geo["lat"], geo["lon"], city)
        if not hotels:
            dispatcher.utter_message(text=f"I couldn't find any hotel listings for {city} right now.")
        else:
            lines = [f"Hotels in {city}:"]
            for h in hotels[:8]:
                eco = "eco-certified" if h.get("eco_tag") else "eco_tag"
                stars = f"{h['stars']} star" if h.get("stars") else "unrated"
                lines.append(f"  - **{h['name']}** - {stars}, {eco}")
            dispatcher.utter_message(text="\n".join(lines))

        dispatcher.utter_message(response="utter_finish_button")
        return reset_fallback_events() + [
            SlotSet("hotel_list_city", None),
            SlotSet("last_checked_city", city),
            SlotSet("awaiting_finish", True),
        ]