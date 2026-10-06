"""
fallback.py — MGH Travel Chatbot
=====================================================================
Custom fallback + human-handover behaviour, replacing Rasa's built-in
action_two_stage_fallback. Current spec (redesigned per the user's
explicit walkthrough of what the fallback system should do — see the
long comment on ActionSmartFallback below for the full behaviour and
the real-execution finding that drove how it's wired):

  * Fires whenever the user says something genuinely unrelated to the
    bot's job — an `out_of_scope` message, or `nlu_fallback` (raised by
    FallbackClassifier when NLU itself can't confidently classify
    anything). It does NOT fire for an invalid-FORMAT answer to a
    question that IS on topic (e.g. typing "banana" for a budget, or a
    city typo) — those keep being handled entirely by that slot's own
    validate_<slot> rejection message in actions.py, with no repeat cap,
    exactly as before. This scoping was the user's own explicit choice.
  * Each time it fires, it checks whether a question is currently
    pending (a form is active and still waiting on its requested_slot):
      - if YES: apologise, then let the very next action be that slot's
        own existing question again (its normal action_ask_<slot> /
        utter_ask_<slot> — unchanged, reused as-is).
      - if NO: state the bot's capabilities in one sentence and show the
        4 main function buttons in the SAME message.
  * After 3 CONSECUTIVE times this fires (whether or not a question was
    pending each time), it apologises ("I didn't understand what you
    meant") and shows a message with a "Clear conversation" button —
    instead of the streak-1/2 message above.
  * Nested with the ORIGINAL whole-conversation give-up cap: every 3rd-
    strike also counts against a separate, never-reset
    `fallback_total_giveups` counter (existing behaviour, kept as-is).
    Once that exceeds GIVEUP_LIMIT_PER_CONVERSATION, the bot escalates
    to a human advisor instead of showing "Clear conversation" — but
    ONLY when no question was pending at the time (see the docstring
    below for why this is deliberately NOT attempted while a form is
    active: forcibly tearing down a half-filled form to hand over is a
    much bigger, riskier change than what was asked for, and isn't
    needed for the escalation to still happen — the same off-topic
    message pattern will keep recurring and will eventually hit this
    branch again once no form is active).

Two counters, both on slots so they survive across turns:
  fallback_streak         — consecutive off-topic/low-confidence turns
                             (resets to 0 on every 3rd strike, and
                             whenever another action in actions.py calls
                             reset_fallback_events() after successfully
                             handling something on-topic).
  fallback_total_giveups  — how many times we've reached the 3rd strike
                             in THIS conversation (never reset).
"""

from __future__ import annotations

import json
import random
import string
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Text

from rasa_sdk import Action, Tracker
from rasa_sdk.events import EventType, FollowupAction, Restarted, SlotSet
from rasa_sdk.executor import CollectingDispatcher

GIVEUP_LIMIT_PER_CONVERSATION = 5
OFFTOPIC_STREAK_LIMIT = 3  # 3rd consecutive off-topic message -> "Clear conversation"

# The two intents that count as "genuinely unrelated to the bot's job"
# for the purposes of this streak — see is_offtopic_intent below.
_OFFTOPIC_INTENTS = {"out_of_scope", "nlu_fallback"}

_CLEAR_CONVERSATION_BUTTON = {"title": "↺ Clear conversation", "payload": "/clear_conversation"}


def is_offtopic_intent(tracker: Tracker) -> bool:
    """True exactly when the CURRENT user message was classified as
    genuinely unrelated to the bot's job (out_of_scope — a trained
    intent for off-topic chit-chat/trivia — or nlu_fallback, raised by
    FallbackClassifier when NLU itself couldn't confidently classify
    anything). Used both here and, via the not_intent restriction added
    to every slot mapping in domain.yml, to decide when a form's own
    from_text catch-all should NOT treat the message as a candidate
    answer at all."""
    intent = (tracker.latest_message or {}).get("intent", {}) or {}
    return intent.get("name") in _OFFTOPIC_INTENTS


def _topic_buttons() -> List[Dict[str, str]]:
    """The bot's own menu of what it can actually help with — the 4
    main functions, exactly as asked ("چهار فانکشن اصلی"). Built in
    Python and sent via dispatcher.utter_message(buttons=...) rather
    than a static domain.yml response — the brief specifically asks for
    buttons "generated dynamically from custom action responses".

    Emojis (real user report): kept identical to streamlit_app.py's own
    main-menu buttons (the sidebar nav + home-screen grid) — the weather
    one used to be ☁️ here vs. 🌤️ there, a mismatch for the same
    function shown two different ways depending on which menu the user
    happened to see."""
    return [
        {"title": "🧳 Plan a trip", "payload": "/ask_trip_planning"},
        {"title": "🌤️ Check the weather", "payload": "/ask_weather"},
        {"title": "💱 Currency exchange", "payload": "/ask_currency_exchange"},
        {"title": "🧑‍💼 Talk to a human advisor", "payload": "/ask_human_advisor"},
    ]


def reset_fallback_events() -> List[EventType]:
    """Any action that successfully handles a genuine, in-scope request
    should append `+ reset_fallback_events()` to its returned events —
    that is how the streak counter learns the user is back on topic."""
    return [SlotSet("fallback_streak", 0)]


class ActionSmartFallback(Action):
    """
    Runs whenever the current message is genuinely off-topic (see
    is_offtopic_intent) — see rules.yml's "Handle off-topic messages
    during <form>" / "Handle low-confidence messages during <form>"
    rules for the form-active case, and the two generic, condition-free
    "Handle ... messages" rules for the no-form-active case.

    REAL-EXECUTION FINDING (this is why the code below reads
    requested_slot directly rather than assuming it's always empty):
    before the domain.yml `not_intent` fix, this action was, in
    practice, ONLY EVER reachable when no form was active — every
    per-form "off-topic during X" rule existed in rules.yml already,
    but could never actually win against the currently-requested
    slot's own from_text catch-all mapping (from_text accepts ANY text
    unconditionally, so slot extraction always "succeeded" with a
    garbage candidate, and Core kept continuing the form instead).
    Confirmed with a real trained model + action server: sending
    "what's the capital of Japan?" (out_of_scope at 99.6% confidence)
    while destination_city was being asked produced ONLY that slot's
    own "I couldn't find a place called '...'" rejection — this action
    never ran, and fallback_streak never moved.

    Now that domain.yml excludes out_of_scope/nlu_fallback from every
    conditioned from_entity/from_text mapping, the rule can actually
    fire while a form is active, WITH active_loop and requested_slot
    still set (the rule's own steps keep the loop active and re-invoke
    the form right after this action runs) — so tracker.active_loop_name
    / tracker.get_slot("requested_slot") correctly reflect the pending
    question here, in the ONE place all of this logic lives, exactly
    matching how the user described the desired behaviour (one shared
    check, not per-slot special-casing).
    """

    def name(self) -> str:
        return "action_smart_fallback"

    def run(self, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict[str, Any]) -> List[EventType]:
        pending_slot: Optional[Text] = tracker.get_slot("requested_slot") if tracker.active_loop_name else None
        return dispatch_offtopic_fallback(dispatcher, tracker, pending_slot)


def dispatch_offtopic_fallback(
    dispatcher: CollectingDispatcher,
    tracker: Tracker,
    pending_slot: Optional[Text],
) -> List[EventType]:
    """Shared decision logic — see ActionSmartFallback's docstring above
    for the full spec this implements.

    PUBLIC (no leading underscore) and imported directly by actions.py
    too: real execution testing found that destination_city, origin_city,
    weather_city, hotel_list_city (all routed through _match_city_or_ask)
    and from_currency each have their OWN custom extract_<slot> method
    that unconditionally takes the raw message text as a candidate,
    completely bypassing domain.yml's `not_intent` restriction (which
    only gates Core's OWN separate, declarative from_entity/from_text
    mapping layer — confirmed these custom extract_<slot> hooks are, in
    practice, NOT bypassed by that restriction, unlike every other slot
    in this project that has no such custom hook). So for those slots,
    this same shared function is called a second way: directly from
    inside their validate_<slot> logic (see actions.py's
    _match_city_or_ask), as a belt-and-suspenders fix that works
    regardless of which of the two extraction layers is what's actually
    producing a candidate on any given turn. Calling this from a
    validate_<slot> context is safe even for slots where the domain.yml
    fix already independently works — this function is then simply
    never reached for those, since the interrupt rule already routes to
    ActionSmartFallback first."""
    streak = int(tracker.get_slot("fallback_streak") or 0) + 1
    total_giveups = int(tracker.get_slot("fallback_total_giveups") or 0)

    if streak < OFFTOPIC_STREAK_LIMIT:
        if pending_slot:
            # Apologise, then do nothing else this turn — the rule that
            # brought us here re-invokes the form right after (see
            # rules.yml), which naturally re-asks THIS EXACT slot's own
            # question again, with its own real text and buttons,
            # completely unchanged. Deliberately not duplicating every
            # slot's question text/buttons here.
            dispatcher.utter_message(
                text="Sorry, that doesn't seem to answer what I just asked — let's get back to it:"
            )
        else:
            dispatcher.utter_message(
                text=(
                    "Sorry, I didn't quite catch that. I'm MGH Travel Chatbot — "
                    "I can plan a full trip, check the weather, convert currencies, "
                    "or connect you with a human advisor. Choose one of these so I can help you:"
                ),
                buttons=_topic_buttons(),
            )
            # BUGFIX (real user report): these 4 buttons send the exact
            # same entry-point payloads (/ask_trip_planning, /ask_weather,
            # ...) as the main menu — which action_route_entry_point
            # refuses to act on while awaiting_finish is still True,
            # printing utter_awaiting_finish_reminder ("...please click
            # Finish...") instead of actually starting that function.
            # awaiting_finish naturally stays True here whenever this
            # fallback fires shortly after a PRIOR flow finished (every
            # flow's own completion sets it, expecting the user to click
            # Finish next) — but this message is showing these exact 4
            # buttons AS the way forward, so leaving the gate up makes
            # them silently do nothing when clicked, confirmed via real
            # execution (clicking any of them printed the Finish
            # reminder instead of routing anywhere). Presenting this
            # message with working buttons IS the fresh start the Finish
            # button would otherwise provide, so clear the same slot
            # action_finish itself clears.
            return [SlotSet("fallback_streak", streak), SlotSet("awaiting_finish", False)]
        return [SlotSet("fallback_streak", streak)]

    # 3rd consecutive off-topic message — give up gracefully, whether or
    # not a question was pending (per the user's own explicit spec).
    total_giveups += 1
    events: List[EventType] = [
        SlotSet("fallback_streak", 0),
        SlotSet("fallback_total_giveups", total_giveups),
    ]

    # Escalating to a live human handover mid-form would mean forcibly
    # tearing down a half-filled form (deactivating it with required
    # slots still missing) — a materially bigger, riskier change than
    # asked for, so it's deliberately only attempted when nothing is
    # pending. The same recurring off-topic pattern will still reach
    # this branch once the user is back at the top level.
    if pending_slot is None and total_giveups > GIVEUP_LIMIT_PER_CONVERSATION:
        dispatcher.utter_message(
            text=(
                "We've hit this a few times now, so rather than keep "
                "guessing, let me bring in a human advisor instead."
            )
        )
        events.append(FollowupAction("action_handover_to_human"))
        return events

    dispatcher.utter_message(
        text="Sorry, I didn't understand what you meant.",
        buttons=[_CLEAR_CONVERSATION_BUTTON],
    )
    return events


class ActionClearConversation(Action):
    """Triggered by the "Clear conversation" button shown on the 3rd
    consecutive off-topic message (see _dispatch_offtopic_fallback
    above) — a payload-only intent, same pattern as
    affirm_city_suggestion/finish. Restarted() wipes this tracker back
    to a blank slate server-side; streamlit_app.py ALSO clears its own
    local session state and starts a fresh sender_id when it sees this
    exact payload, mirroring exactly what it already does for /finish.
    """

    def name(self) -> str:
        return "action_clear_conversation"

    def run(self, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict[str, Any]) -> List[EventType]:
        return [Restarted()]


# -------------------------------------------------------------------------
# Human handover — full-context packaging
# -------------------------------------------------------------------------

_HANDOVER_SLOT_KEYS = [
    "origin_city", "destination_city", "travel_date_from", "travel_date_to",
    "num_travelers", "budget_amount", "budget_currency", "sustainability_level",
    "priority_focus",
]


def _build_transcript(tracker: Tracker, max_turns: int = 20) -> List[str]:
    transcript = []
    for event in tracker.events:
        if event.get("event") == "user" and event.get("text"):
            transcript.append(f"USER: {event['text']}")
        elif event.get("event") == "bot" and event.get("text"):
            transcript.append(f"BOT : {event['text']}")
    return transcript[-max_turns:]


def _collected_slots(tracker: Tracker) -> Dict[str, Any]:
    return {k: tracker.get_slot(k) for k in _HANDOVER_SLOT_KEYS if tracker.get_slot(k) is not None}


def send_to_human_queue(package: Dict[str, Any]) -> None:
    """Coursework-honest stand-in for a real integration. A production
    system would POST this package to a Slack webhook or a ticketing
    system (Zendesk, Freshdesk, ...). That infrastructure doesn't exist
    for a demo, so it is printed to the action server's console instead,
    for a human advisor to pick up right away — and deliberately NOT
    written to disk anywhere. This package can carry the user's contact
    info and conversation transcript, and persisting that indefinitely
    with no retention/deletion policy would conflict with this project's
    "no persistent user data" requirement; the console is transient by
    nature, so nothing about this handover is stored once it scrolls past.
    Being explicit about the console-only substitution in the report is
    legitimate; presenting a console log as a finished integration would
    not be — see "Making a Bot Behave" §5.3.
    """
    print("\n===== HUMAN HANDOVER =====")
    print(json.dumps(package, indent=2, ensure_ascii=False))
    print("===========================\n")


class ActionHandoverToHuman(Action):
    def name(self) -> str:
        return "action_handover_to_human"

    def run(self, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict[str, Any]):
        last_intent = tracker.latest_message.get("intent", {}).get("name")
        last_confidence = tracker.latest_message.get("intent", {}).get("confidence")
        transcript = _build_transcript(tracker)
        reference = "HUM-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=6))

        package = {
            "handover_reference": reference,
            "conversation_id": tracker.sender_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "collected_slots": _collected_slots(tracker),
            "trip_results_available": tracker.get_slot("trip_results") is not None,
            "last_intent": last_intent,
            "last_confidence": last_confidence,
            "turn_count": len(transcript),
            "transcript": transcript,
        }
        send_to_human_queue(package)

        dispatcher.utter_message(response="utter_handover_notice")
        dispatcher.utter_message(
            text=(
                f"Your reference for this handover is **{reference}** — a human "
                "advisor can pull up everything we've discussed using it."
            ),
            # json_message IS the custom payload — CollectingDispatcher sets
            # "custom": json_message or {} directly, so the old extra
            # {"custom": ...} wrapper double-nested this and made it fall
            # through to a raw JSON dump in streamlit_app.py instead of a
            # rendered card. See actions/actions.py for the same fix.
            json_message={"card_type": "handover", "reference": reference},
        )
        return [SlotSet("handover_active", True)]