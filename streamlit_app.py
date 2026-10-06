"""
streamlit_app.py — MGH Travel Chatbot dashboard (minimal relay)
=====================================================================
DELIBERATELY DUMB, BY DESIGN. This file contains ZERO business logic —
no scoring, no tier-colour decisions, no card layout logic, nothing
that decides what the "right" answer or presentation is. All of that
already lives entirely in the backend (actions/scoring.py,
actions/actions.py). This file does exactly three things:

  1. Capture what the user typed, or which button they clicked
     (a Rasa payload string like '/confirm_booking').
  2. POST it to Rasa's REST webhook.
  3. Display whatever comes back — text as text, buttons as buttons,
     and any `custom` JSON payload as a small rendered card for known
     card types (falling back to raw JSON for anything unrecognised).
"""

import os
import uuid

import requests
import streamlit as st

RASA_URL = os.environ.get("RASA_URL", "http://localhost:5005")
REST_WEBHOOK = f"{RASA_URL}/webhooks/rest/webhook"

st.set_page_config(page_title="MGH Travel Chatbot", page_icon="✦")

# Pure CSS theming — Light white background with Eco-Green accents and buttons.
# This is presentation only: it doesn't change what's shown or when.
st.markdown(
    """
<style>
/* ================================================================
   MGH Travel Chatbot — VISUAL THEME ONLY
   No Rasa/business/state logic is changed by this section.
   Palette:
     White        #FFFFFF  — background
     Black        #111111  — typography
     Fresh Green  #7CF7A8  — bot
     Persian Blue #20B8C9  — user
   ================================================================ */

:root {
    --eco-white: #FFFFFF;
    --eco-black: #111111;
    --eco-muted: #667085;
    --eco-green: #7CF7A8;
    --eco-green-deep: #35C878;
    --eco-green-soft: #E9FFF1;
    --eco-blue: #20B8C9;
    --eco-blue-deep: #1197AA;
    --eco-blue-soft: #E7FBFE;
    --eco-border: #E7EAEE;
    --eco-shadow: 0 10px 30px rgba(17, 17, 17, 0.07);
}


/* ---------- Streamlit chrome: seamless white canvas ---------- */
header[data-testid="stHeader"],
header {
    background: #FFFFFF !important;
    border-bottom: 1px solid #F1F3F5 !important;
}

[data-testid="stToolbar"] {
    background: #FFFFFF !important;
}

[data-testid="stBottom"],
[data-testid="stBottomBlockContainer"],
.stBottomBlockContainer {
    background: #FFFFFF !important;
    border-top: 1px solid #F1F3F5 !important;
    box-shadow: 0 -8px 22px rgba(17, 17, 17, 0.035) !important;
}

[data-testid="stBottom"] > div,
[data-testid="stBottomBlockContainer"] > div,
.stBottomBlockContainer > div {
    background: #FFFFFF !important;
}

/* ---------- Sidebar: persistent panel, same white canvas ---------- */
[data-testid="stSidebar"] {
    background: #FFFFFF !important;
    border-right: 1px solid #F1F3F5 !important;
}

[data-testid="stSidebar"] * {
    color: var(--eco-black) !important;
}

[data-testid="stSidebar"] .stButton > button {
    border-color: var(--eco-green-deep) !important;
}

/* ---------- Global canvas ---------- */
.stApp {
    background: var(--eco-white) !important;
    color: var(--eco-black) !important;
}

.stApp,
.stApp * {
    font-family: Inter, ui-sans-serif, -apple-system, BlinkMacSystemFont,
                 "Segoe UI", sans-serif;
}

.block-container {
    max-width: 900px !important;
    padding-top: 2.4rem !important;
    padding-bottom: 7rem !important;
}

/* ---------- Elegant header ---------- */
h1 {
    color: var(--eco-black) !important;
    font-size: clamp(2rem, 5vw, 3rem) !important;
    font-weight: 800 !important;
    letter-spacing: -0.045em !important;
    line-height: 1.05 !important;
    margin-bottom: 0.25rem !important;
}

.eco-subtitle {
    color: var(--eco-muted);
    font-size: 0.95rem;
    margin: 0 0 1.6rem 0;
    letter-spacing: 0.01em;
}

.eco-accent {
    display: inline-block;
    width: 46px;
    height: 5px;
    border-radius: 999px;
    background: linear-gradient(90deg, var(--eco-green), var(--eco-blue));
    margin: 0.15rem 0 0.8rem 0;
}

/* ---------- Chat area ---------- */
[data-testid="stChatMessage"] {
    border-radius: 22px !important;
    border: 1px solid var(--eco-border) !important;
    padding: 0.9rem 1.05rem !important;
    margin: 0.65rem 0 !important;
    box-shadow: var(--eco-shadow) !important;
    color: var(--eco-black) !important;
    transition: transform 0.18s ease, box-shadow 0.18s ease;
}

[data-testid="stChatMessage"]:hover {
    transform: translateY(-1px);
    box-shadow: 0 14px 34px rgba(17, 17, 17, 0.09) !important;
}

/* Bot = fresh luminous green */
[data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarAssistant"]) {
    background: linear-gradient(135deg, #F4FFF8 0%, var(--eco-green-soft) 100%) !important;
    border-color: #C8F7D7 !important;
}

/* User = Persian/turquoise blue */
[data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarUser"]) {
    background: linear-gradient(135deg, var(--eco-blue-soft) 0%, #F5FEFF 100%) !important;
    border-color: #BDEEF3 !important;
}

/* Message text */
[data-testid="stChatMessage"] [data-testid="stMarkdownContainer"],
[data-testid="stChatMessage"] p,
[data-testid="stChatMessage"] li {
    color: var(--eco-black) !important;
    line-height: 1.65 !important;
}

[data-testid="stChatMessage"] strong {
    color: var(--eco-black) !important;
    font-weight: 750 !important;
}

/* ---------- Chat avatars ---------- */
[data-testid="stChatMessageAvatarAssistant"] {
    background: var(--eco-green) !important;
    color: var(--eco-black) !important;
    border: 2px solid #C9FBDD !important;
}

[data-testid="stChatMessageAvatarUser"] {
    background: var(--eco-blue) !important;
    color: var(--eco-white) !important;
    border: 2px solid #B9EAF0 !important;
}

/* ---------- Choice buttons ---------- */
.stButton > button {
    background: var(--eco-white) !important;
    color: var(--eco-black) !important;
    border: 1.5px solid var(--eco-green-deep) !important;
    border-radius: 999px !important;
    min-height: 2.65rem !important;
    padding: 0.35rem 1.1rem !important;
    font-weight: 700 !important;
    letter-spacing: 0.005em !important;
    box-shadow: 0 4px 12px rgba(53, 200, 120, 0.12) !important;
    transition: all 0.18s ease !important;
    width: 100% !important;
}

.stButton > button:hover {
    background: var(--eco-green) !important;
    color: var(--eco-black) !important;
    border-color: var(--eco-green-deep) !important;
    transform: translateY(-2px);
    box-shadow: 0 8px 18px rgba(53, 200, 120, 0.20) !important;
}

.stButton > button:active {
    transform: translateY(0);
}

/* Chat-message buttons (Yes/No, city suggestions, currency pairs, date
   options, ...) are laid out by streamlit_app.py itself, in Python —
   see the chat-history loop, which splits every button row into its
   own st.columns(len(row_buttons)) group so buttons stay horizontal
   and wrap to a new row of columns instead of stacking one per line.
   BUGFIX (real user report + screenshot: a button "escaping its box",
   uneven spacing): this used to be overridden here a SECOND time, back
   when buttons had no Python-side column layout at all and this
   display:inline-block + width:auto CSS hack was the ONLY thing making
   them sit side by side. Once the st.columns()-per-row layout above was
   added, this leftover override started fighting it instead — width:
   auto !important on the button undid use_container_width=True's
   "fill this column" sizing, and display:inline-block !important on
   its wrapper undid the column's own layout box, so any button whose
   label was wider than its column's share (see the 3-button
   sustainability/priority questions) rendered at its full natural
   width and overflowed past the column, and past the chat bubble
   itself, instead of filling or wrapping within its assigned column.
   Removed — buttons inside a chat message now simply inherit the
   global `.stButton > button { width: 100% !important; ... }` rule
   above, filling exactly the column Python already sized for them,
   the same mechanism the home-screen/sidebar buttons already use
   correctly. */

/* Clear button gets a quieter appearance. Border colour only — every
   st.button() in this app renders as kind="secondary" (Streamlit's own
   default when no `type=` is passed), so this selector actually matches
   EVERY button app-wide, not just the sidebar's Clear button. It used to
   also force `width: auto !important` here, which — being more specific
   than the plain `.stButton > button` rule above — silently overrode
   that rule's `width: 100%` for every button in the app, including
   chat-message option buttons, and was a second source of the same
   overflow bug described above. Dropped; width is governed solely by
   `.stButton > button` (100%, filling whatever column Python/
   use_container_width assigned) now. */
div[data-testid="stButton"] > button[kind="secondary"] {
    border-color: #D9DEE5 !important;
}

/* ---------- Chat input ---------- */
[data-testid="stChatInput"] {
    border-radius: 22px !important;
}

[data-testid="stChatInput"] > div {
    background: var(--eco-white) !important;
    border: 1.5px solid #DDE3E8 !important;
    border-radius: 22px !important;
    box-shadow: 0 12px 35px rgba(17, 17, 17, 0.09) !important;
    transition: border-color 0.18s ease, box-shadow 0.18s ease;
}

[data-testid="stChatInput"] > div:focus-within {
    border-color: var(--eco-blue) !important;
    box-shadow: 0 0 0 4px rgba(32, 184, 201, 0.12),
                0 12px 35px rgba(17, 17, 17, 0.09) !important;
}

/* ---------- Send button inside Chat Input ---------- */
[data-testid="stChatInput"] button {
    color: var(--eco-green-deep) !important;
    background-color: transparent !important;
    border: none !important;
}

[data-testid="stChatInput"] button:hover {
    color: var(--eco-blue) !important;
    background-color: var(--eco-green-soft) !important;
}

[data-testid="stChatInput"] button svg {
    fill: currentColor !important;
}

[data-testid="stChatInput"] textarea,
[data-testid="stChatInput"] input {
    background: transparent !important;
    color: #111111 !important; /* رنگ متن تایپ‌شده */
    -webkit-text-fill-color: #111111 !important; /* برای پشتیبانی در مرورگرهای Chrome/Safari */
    caret-color: #111111 !important; /* رنگ کرسر چشمک‌زن مشکی */
    font-size: 1rem !important;
}

/* رنگ متن راهنما (Ask about your next...) */
[data-testid="stChatInput"] textarea::placeholder,
[data-testid="stChatInput"] input::placeholder {
    color: #667085 !important;
    -webkit-text-fill-color: #667085 !important;
}

/* ---------- JSON custom payload ---------- */
[data-testid="stJson"] {
    background: #FAFBFC !important;
    border: 1px solid var(--eco-border) !important;
    border-radius: 16px !important;
    padding: 0.35rem !important;
}

/* ---------- Spinner ---------- */
[data-testid="stSpinner"] {
    color: var(--eco-green-deep) !important;
}

/* ---------- Subtle scrollbar ---------- */
::-webkit-scrollbar {
    width: 8px;
}
::-webkit-scrollbar-track {
    background: var(--eco-white);
}
::-webkit-scrollbar-thumb {
    background: #DDE5E1;
    border-radius: 999px;
}
::-webkit-scrollbar-thumb:hover {
    background: #BFD4C7;
}

/* ---------- Mobile refinement ---------- */
@media (max-width: 640px) {
    .block-container {
        padding: 1.2rem 0.8rem 6rem 0.8rem !important;
    }

    [data-testid="stChatMessage"] {
        border-radius: 18px !important;
        padding: 0.75rem 0.8rem !important;
    }

    h1 {
        font-size: 2rem !important;
    }
}
</style>
""",
    unsafe_allow_html=True,
)

if "sender_id" not in st.session_state:
    st.session_state.sender_id = str(uuid.uuid4())
if "messages" not in st.session_state:
    st.session_state.messages = []  # list of {role, text, buttons, custom}
if "pending_payload" not in st.session_state:
    st.session_state.pending_payload = None
if "pending_display_text" not in st.session_state:
    st.session_state.pending_display_text = None


def send_to_rasa(message: str, display_text: str) -> None:
    """Sends `message` (raw text OR a Rasa button payload like
    '/inform_num_travelers{"num_travelers": "1"}') to the REST webhook,
    but shows `display_text` (e.g. the button's title, "Just me") in the
    chat history instead — the payload is what Rasa needs, not what a
    human should see printed back at them."""
    st.session_state.messages.append({"role": "user", "text": display_text, "buttons": None, "custom": None})
    try:
        response = requests.post(
            REST_WEBHOOK,
            json={"sender": st.session_state.sender_id, "message": message},
            timeout=60,
        )
        response.raise_for_status()
        bot_replies = response.json()
    except requests.exceptions.RequestException as exc:
        st.session_state.messages.append(
            {"role": "assistant", "text": f"⚠️ Could not reach the bot server: {exc}", "buttons": None, "custom": None}
        )
        return

    if not bot_replies:
        st.session_state.messages.append(
            {"role": "assistant", "text": "(bot sent no response)", "buttons": None, "custom": None}
        )
        return

    for reply in bot_replies:
        custom = reply.get("custom")
        if custom and custom.get("card_type") == "reset_to_menu":
            # Declining "Would you like to book this?" (actions.py's
            # ActionDenyBooking) resets the conversation back to the
            # initial menu state, exactly like the "Clear conversation"
            # button — but unlike that button, this can be triggered by
            # arbitrary typed text ("no need", "not now", ...), not just
            # a payload this frontend already knows in advance, so it
            # can't be special-cased before sending like "/finish"/
            # "/clear_conversation" are below. Watching for this marker
            # on the REPLY instead achieves the same reset regardless of
            # how the user phrased their answer. Nothing is appended to
            # the visible history — it carries no text of its own.
            st.session_state.messages = []
            st.session_state.sender_id = str(uuid.uuid4())
            continue
        st.session_state.messages.append(
            {
                "role": "assistant",
                "text": reply.get("text"),
                "buttons": reply.get("buttons"),
                "custom": custom,
            }
        )


# ---------- Shared on_click callbacks for every button in this app ---------
# Streamlit calls on_click callbacks BEFORE the script body re-executes
# (documented behaviour, part of how it processes any widget
# interaction) — that timing is what actually eliminates the "flash"
# seen when a button's own state-setting used to happen INSIDE an
# `if st.button(...):` block instead. With that pattern, the button
# (and everything drawn above it — e.g. the whole home-screen grid)
# had ALREADY been sent to the browser as part of rendering this run,
# before the script ever reached the click-handling code — so an
# explicit st.rerun() there could only fix the NEXT run, never the one
# where the click itself just happened. Moving the state change into
# on_click means it happens first, so by the time the script body runs
# and reaches any layout decision depending on that state (e.g. the
# home-screen's `if not st.session_state.messages...` guard below), the
# updated value is already in place — the stale layout is never drawn
# in the first place, not even for one frame. No explicit st.rerun() is
# needed here either — Streamlit already reruns the script once after
# any widget interaction, callback or not.
def _queue_payload(payload: str, display_text: str) -> None:
    st.session_state.pending_payload = payload
    st.session_state.pending_display_text = display_text


def _reset_and_queue_payload(payload: str, display_text: str) -> None:
    """Same as _queue_payload, but also resets the conversation first —
    used by the sidebar's main-flow buttons, which behave like Clear
    conversation immediately followed by picking that flow."""
    st.session_state.messages = []
    st.session_state.sender_id = str(uuid.uuid4())
    st.session_state.pending_payload = payload
    st.session_state.pending_display_text = display_text


def _clear_conversation() -> None:
    st.session_state.messages = []
    st.session_state.sender_id = str(uuid.uuid4())


# The exact 4 bare (no-JSON) entry-point payloads that action_smart_fallback's
# own "I don't know what you mean, pick one" message offers via
# actions/fallback.py's _topic_buttons() — the SAME payload strings as the
# sidebar's own 4 main-flow buttons (_NAV_ITEMS above). Real user request: a
# click on one of THESE in-chat buttons should behave exactly like the
# sidebar buttons — clear conversation, then run that flow fresh — not like
# every other in-chat button (currency-pair suggestions, city suggestions,
# day-of-week quick replies, yes/no confirmations, ...), which must keep
# answering the CURRENT form/question instead of resetting it. Those other
# buttons all send one of these same 4 intents too, but always WITH a JSON
# payload attached (e.g. `/ask_trip_planning{"destination_city": "Rome"}` or
# `/ask_currency_exchange{"from_currency": "EUR", "to_currency": "USD"}`), so
# matching on the bare string only (no "{" in it) is exact and doesn't need
# to look at anything but the payload itself.
_MAIN_FLOW_ENTRY_PAYLOADS = {
    "/ask_trip_planning",
    "/ask_weather",
    "/ask_currency_exchange",
    "/ask_human_advisor",
}


# =============================================================================
# Card rendering — purely presentational: every value here (price, tier,
# score, disclosure text) was already decided by the backend
# (actions/scoring.py, actions/actions.py). This just lays it out nicely
# instead of dumping raw JSON. Tier colours are semantic (green/amber/red
# = low/moderate/high impact, per the brief) and intentionally distinct
# from the bot/user role colours in the CSS above.
# =============================================================================

TIER_COLORS = {"green": "#22C55E", "amber": "#F59E0B", "red": "#EF4444"}
TIER_LABELS = {"green": "Low impact", "amber": "Moderate", "red": "High impact"}


def _tier_badge(tier: str) -> str:
    color = TIER_COLORS.get(tier, "#9CA3AF")
    label = TIER_LABELS.get(tier, "")
    return (
        f'<span style="background:{color}22;color:{color};border:1px solid {color};'
        f'font-weight:600;font-size:0.75rem;padding:2px 10px;border-radius:999px;">{label}</span>'
    )


def render_trip_summary(data: dict) -> None:
    st.markdown(f"#### {data.get('origin', '')} → {data.get('destination', '')}")

    travel_options = data.get("travel_options") or []
    if travel_options:
        st.markdown("**Getting there**")
        for opt in travel_options:
            price = f"~{opt['price']:.0f} EUR" if opt.get("price") is not None else "fare not available"
            c1, c2, c3 = st.columns([3, 3, 2])
            c1.markdown(f"**{opt['mode'].capitalize()}**  \n{price}")
            c2.markdown(f"~{opt['carbon_kg']:.0f} kg CO2e")
            c3.markdown(_tier_badge(opt.get("tier", "")), unsafe_allow_html=True)

    nights = data.get("nights", 3)

    hotels = data.get("hotels") or []
    if hotels:
        st.markdown("**Where to stay**")
        for h in hotels[:3]:
            eco = "✓ eco-certified" if h.get("eco_certified") else "no eco-certification listed"
            c1, c2, c3 = st.columns([3, 3, 2])
            c1.markdown(f"**{h['name']}**  \n{eco}")
            c2.markdown(f"~{h['price']:.0f} EUR / {nights} night{'s' if nights != 1 else ''}")
            c3.markdown(_tier_badge(h.get("tier", "")), unsafe_allow_html=True)

    # CHANGED (explicit user request): forecast_days already only ever
    # contains min(nights, MAX_FORECAST_DAYS_SHOWN) days — see
    # actions.py's ActionGenerateTripRecommendations.run, which requests
    # exactly that many days from get_weather_forecast (MAX_FORECAST_DAYS_
    # SHOWN = 5 there). So a stay under 5 nights already shows weather for
    # every night with no change needed. For a stay of 5+ nights, the list
    # itself was already correctly capped at 5 days — what was missing was
    # telling the user this is only the FIRST 5 days of a longer stay,
    # instead of silently looking like the whole trip's forecast.
    forecast_days = data.get("forecast_days") or []
    if forecast_days:
        if nights >= 5:
            st.markdown("**Weather estimate for the first 5 days of your trip**")
        else:
            st.markdown("**Weather during your stay**")
        for day in forecast_days:
            note = " (seasonal estimate)" if day.get("source") == "seasonal_estimate" else ""
            st.markdown(f"- {day['date']}: ~{day['temperature_max_c']:.0f}°C{note}")

    st.markdown(f"**Public transport:** {data.get('transport_summary', 'n/a')}")

    attractions = data.get("attractions") or []
    if attractions:
        st.markdown("**Worth visiting**")
        for a in attractions:
            if a.get("summary"):
                st.markdown(f"- **{a['name']}** — {a['summary']}")
            else:
                st.markdown(f"- **{a['name']}**")


def render_more_hotels(hotels: list) -> None:
    for h in hotels:
        eco = "✓ eco-certified" if h.get("eco_certified") else "no eco-certification listed"
        c1, c2, c3 = st.columns([3, 3, 2])
        c1.markdown(f"**{h['name']}**  \n{eco}")
        c2.markdown(f"~{h['price']:.0f} EUR / stay")
        c3.markdown(_tier_badge(h.get("tier", "")), unsafe_allow_html=True)


def render_handover(payload: dict) -> None:
    # Same reference the backend's own text message already states
    # (actions/fallback.py's ActionHandoverToHuman) — this card was
    # previously falling through to the raw-JSON block because
    # "handover" had no entry in _CARD_RENDERERS at all (a second,
    # separate bug from the json_message double-nesting fix in
    # actions.py/fallback.py: even once that nesting was corrected,
    # this card_type still had nowhere registered to render it).
    ref = payload.get("reference", "")
    st.info(
        f"🧑‍💼 Your reference for this handover is **{ref}** — a human "
        "advisor can pull up everything we've discussed using it."
    )


_CARD_RENDERERS = {
    "trip_summary": lambda c: render_trip_summary(c.get("data", {})),
    "more_hotels": lambda c: render_more_hotels(c.get("data", [])),
    "handover": render_handover,
}


def render_custom(custom: dict) -> bool:
    """Renders a card if the type is recognised (returns True, so the
    caller skips the redundant plain-text version). Falls back to the
    styled raw-JSON block (see the CSS `[data-testid="stJson"]` rule
    above) for any card_type it doesn't know yet, so nothing silently
    vanishes if the backend adds a new one."""
    renderer = _CARD_RENDERERS.get(custom.get("card_type"))
    if renderer:
        renderer(custom)
        return True
    st.json(custom)
    return True


st.markdown(
    """
    <div style="display:flex;align-items:center;gap:14px;margin-bottom:2px;">
        <div style="
            width:52px;height:52px;border-radius:17px;
            display:flex;align-items:center;justify-content:center;
            background:linear-gradient(135deg,#7CF7A8 0%,#20B8C9 100%);
            box-shadow:0 8px 22px rgba(32,184,201,.16);
            font-size:27px;
        ">✦</div>
        <div>
            <h1 style="margin:0 !important;">MGH Travel Chatbot</h1>
        </div>
    </div>
    <div class="eco-accent"></div>
    """,
    unsafe_allow_html=True,
)

# BUGFIX (real user request): the intro description below the header
# ("Plan smarter journeys..." / "I can plan a full low-carbon trip...")
# is only meant for the very first, empty-chat screen — once the user
# has typed anything or clicked any button (a message exists, or one is
# in flight via pending_payload), it should disappear. Reuses the SAME
# "is this the empty home screen" condition already used just below for
# the 4-main-flow button grid (`if not st.session_state.messages:`),
# rather than inventing a second, possibly-inconsistent way to detect
# "first screen" — and also checks pending_payload so it hides the
# instant a button click is in flight, not only once the bot's first
# reply lands (matching how the button grid below hides itself at that
# same moment too).
if not st.session_state.messages and not st.session_state.pending_payload:
    st.markdown(
        """
        <div class="eco-subtitle">Plan smarter journeys with a lighter footprint — get travel guidance, check weather conditions, explore currency exchange rates, and make better-informed trip decisions in one place.

   I can plan a full low-carbon trip end-to-end, check the weather anywhere, convert currencies, or connect you with a human advisor. Pick one below, or just tell me where you're headed.</div>
        """,
        unsafe_allow_html=True,
    )

# ---------- Persistent sidebar controls ----------
# Lives in st.sidebar rather than the main column so it never scrolls
# away as the chat history grows — a fixed panel, not part of the
# scrolling page flow.
with st.sidebar:
    st.markdown("### MGH Travel Chatbot")

    # The 4 main flows, always available here (not just on the empty
    # home screen) — clicking one resets the conversation first, then
    # immediately runs that flow, exactly like starting fresh and
    # picking it from the home screen would.
    _sidebar_nav_css = """
        <style>
        [data-testid="stSidebar"] div[data-testid="stButton"] button {
            font-size: 0.8rem !important;
            padding: 0.3rem 0.7rem !important;
            min-height: 2.1rem !important;
        }
        </style>
    """
    st.markdown(_sidebar_nav_css, unsafe_allow_html=True)

    _NAV_ITEMS = [
        ("🧳 Trip Assistant & Booking", "/ask_trip_planning", "Trip Assistant & Booking"),
        ("🌤️ Weather Checking", "/ask_weather", "Weather Checking"),
        ("💱 Currency Exchange", "/ask_currency_exchange", "Currency Exchange"),
        ("🧑‍💼 Human Advisor", "/ask_human_advisor", "Human Advisor"),
    ]
    for _label, _payload, _display in _NAV_ITEMS:
        st.button(
            _label, key=f"nav_{_payload}", use_container_width=True,
            on_click=_reset_and_queue_payload, args=(_payload, _display),
        )

    st.button(
    "↺  Clear conversation / Main menu",
    type="secondary",
    use_container_width=True,
    on_click=_clear_conversation
    )
    # Purely explanatory — no logic here, just documenting decisions the
    # backend already makes (scoring.py, transport.py) so the user knows
    # what they're about to see before any trip is planned. Small font,
    # sits below the 4 main flows + Clear button per the sidebar's
    # tighter space (navigation first, reference info after).
    st.markdown(
        """
        <div style="border:1px solid #E7EAEE;border-radius:12px;padding:0.6rem 0.75rem;
                    background:#FAFBFC;font-size:0.68rem;color:#475467;margin:0.8rem 0;">
            <b style="color:#111111;font-size:0.7rem;">What the badges mean</b>
            <table style="width:100%;margin-top:0.3rem;border-collapse:collapse;">
                <tr><td>🟢 Low impact</td><td style="text-align:right;">greenest / cheapest</td></tr>
                <tr><td>🟡 Moderate</td><td style="text-align:right;">middle of the range</td></tr>
                <tr><td>🔴 High impact</td><td style="text-align:right;">highest carbon or cost</td></tr>
            </table>
            <hr style="border:none;border-top:1px solid #E7EAEE;margin:0.5rem 0;">
            <b style="color:#111111;font-size:0.7rem;">How we rank things</b>
            <p style="margin:0.25rem 0 0 0;">
                Your sustainability preference weighs carbon footprint vs. price when
                ranking hotels and travel options. Public transport quality near your
                destination is factored in the same way when comparing travel modes.
            </p>
        </div>
        """,
        unsafe_allow_html=True,
    )


# ---------- Home screen: 4 main flows + sub-buttons ----------
# Only shown while the chat is empty (fresh load or after Clear) AND no
# click is already in flight — reaching the `else` below instead. The
# on_click fix from before solved a DIFFERENT problem (a stale click
# being processed a turn late); what's left is a plain layout-continuity
# issue: when pending_payload is set, this whole block used to render
# NOTHING at all here, while the "Thinking…" spinner (called much later
# in the script, near the chat input) appeared in a completely different
# spot — that gap-then-reappear-elsewhere IS the visible "jump", not a
# rendering bug. Showing a placeholder in this SAME spot instead keeps
# the page's layout continuous across the transition.
if not st.session_state.messages:
    if st.session_state.pending_payload:
        st.markdown(
            "<div style='padding:2.4rem 0; text-align:center; color:#667085; font-size:0.95rem;'>",
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            "<p style='color:#667085; font-size:0.95rem; margin-top:0.4rem; margin-bottom:1.2rem;'>"

            "</p>",
            unsafe_allow_html=True,
        )

        col1, col2, col3, col4 = st.columns(4)

        with col1:
            st.button("🧳 Trip Assistant\n& Booking", key="home_trip",
                       on_click=_queue_payload, args=("/ask_trip_planning", "Trip Assistant & Booking"))
            st.button("🏨 Hotel list", key="home_hotel_list",
                       on_click=_queue_payload, args=("/see_hotel_list", "Hotel list"))
            st.button("🗺️ Cities list", key="home_cities_list",
                       on_click=_queue_payload, args=("/see_cities_list", "Cities list"))

        with col2:
            st.button("🌤️ Weather\nChecking", key="home_weather",
                       on_click=_queue_payload, args=("/ask_weather", "Weather Checking"))

        with col3:
            st.button("💱 Currency\nExchange", key="home_currency",
                       on_click=_queue_payload, args=("/ask_currency_exchange", "Currency Exchange"))
            st.button("EUR → USD", key="home_eur_usd",
                       on_click=_queue_payload,
                       args=('/ask_currency_exchange{"from_currency": "EUR", "to_currency": "USD"}', "EUR → USD"))
            st.button("USD → EUR", key="home_usd_eur",
                       on_click=_queue_payload,
                       args=('/ask_currency_exchange{"from_currency": "USD", "to_currency": "EUR"}', "USD → EUR"))

        with col4:
            st.button("🧑‍💼 Human\nAdvisor", key="home_advisor",
                       on_click=_queue_payload, args=("/ask_human_advisor", "Human Advisor"))

last_assistant_idx = max(
    (i for i, m in enumerate(st.session_state.messages) if m["role"] == "assistant"),
    default=-1,
)

for i, msg in enumerate(st.session_state.messages):
    with st.chat_message(msg["role"]):
        card_drawn = False
        if msg.get("custom") is not None:
            card_drawn = render_custom(msg["custom"])
        if msg.get("text") and not card_drawn:
            st.write(msg["text"])
        # Only the most recent bot message's buttons stay clickable —
        # older ones are shown as plain history.

        # Only the most recent bot message's buttons stay clickable.
        # Buttons are arranged horizontally and wrap automatically.
        if msg.get("buttons") and i == last_assistant_idx:
            buttons = msg["buttons"]

            # Put as many buttons as possible on each row.
            # Streamlit columns are used so buttons stay horizontal
            # instead of becoming one full-width button per line.
            num_buttons = len(buttons)

            if num_buttons <= 2:
                columns_per_row = num_buttons
            elif num_buttons <= 4:
                columns_per_row = num_buttons
            elif num_buttons <= 6:
                columns_per_row = 3
            elif num_buttons <= 10:
                columns_per_row = 5
            else:
                columns_per_row = 5

            for row_start in range(0, num_buttons, columns_per_row):
                row_buttons = buttons[row_start:row_start + columns_per_row]
                cols = st.columns(len(row_buttons))

                for col_offset, (col, btn) in enumerate(zip(cols, row_buttons)):
                    with col:
                        # One of action_smart_fallback's own 4 main-flow
                        # buttons (see _MAIN_FLOW_ENTRY_PAYLOADS above)
                        # behaves exactly like the sidebar's main-flow
                        # buttons: clear conversation, then run that flow
                        # fresh. Every other in-chat button (suggestions,
                        # day-of-week quick replies, yes/no confirmations,
                        # ...) keeps answering the current question instead.
                        _on_click = (
                            _reset_and_queue_payload
                            if btn["payload"] in _MAIN_FLOW_ENTRY_PAYLOADS
                            else _queue_payload
                        )
                        st.button(
                            btn["title"],
                            # Includes the button's own position within the
                            # row (col_offset), not just where the row
                            # started — two buttons sharing an identical
                            # payload (a real bug found and fixed
                            # separately, in actions.py's date-option list)
                            # would otherwise collide on the SAME key and
                            # crash the whole app with a DuplicateWidgetID
                            # error. This is defense in depth: the root
                            # cause is fixed too, but a key collision
                            # should never be fatal on its own.
                            key=f"btn-{i}-{row_start + col_offset}-{btn['payload']}",
                            use_container_width=True,
                            on_click=_on_click,
                            args=(btn["payload"], btn["title"]),
                        )

user_text = st.chat_input("Ask about your next eco-friendly journey…")
if user_text:
    st.session_state.pending_payload = user_text
    st.session_state.pending_display_text = user_text

if st.session_state.pending_payload:
    payload = st.session_state.pending_payload
    display_text = st.session_state.pending_display_text
    st.session_state.pending_payload = None
    st.session_state.pending_display_text = None
    with st.spinner("Thinking …"):
        send_to_rasa(payload, display_text)
    if payload in ("/finish", "/clear_conversation"):
        # "/finish" acts exactly like the Clear conversation button (see
        # the comment that used to live here). "/clear_conversation" IS
        # the Clear conversation button — the one shown by the bot
        # itself after 3 consecutive off-topic messages (see
        # actions/fallback.py) — handled identically to the sidebar's
        # own _clear_conversation(): reset the chat immediately rather
        # than leaving the bot's own reply sitting in the transcript.
        st.session_state.messages = []
        st.session_state.sender_id = str(uuid.uuid4())
    st.rerun()