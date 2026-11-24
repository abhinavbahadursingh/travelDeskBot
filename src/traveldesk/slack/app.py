"""Slack /traveldesk bot — employee self-booking.

Flow:
  1. Employee runs `/traveldesk Delhi to Mumbai Tuesday morning`.
  2. Claude (with Haiku/Sonnet routing) picks a flight from a LetsFG REST
     search by BOOKING_RULE / MAX_PRICE.
  3. The chosen flight is posted back to the **requesting employee** in DM
     with Confirm / Cancel buttons (Slack Block Kit).
  4. Only the requesting employee can confirm their own request. Duplicate
     clicks and Slack event retries are ignored atomically.
  5. On confirm, we call the provider to book. If LetsFG returns an approval
     URL we post it to the employee; otherwise we poll to the PNR.
  6. The employee is never told payment has succeeded until LetsFG confirms it.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import UTC, date, datetime, timedelta

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from traveldesk import agents as agent_factory
from traveldesk import config
from traveldesk.booking import flight_scope, letsfg_booking, traveller_profile
from traveldesk.clients import letsfg_rest
from traveldesk.storage import db, secure_store

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logging.getLogger("slack_sdk").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("traveldesk")

# token_verification_enabled=False lets unit tests import this module with
# a stub token; real token errors still surface on the first API call.
app = App(token=config.SLACK_BOT_TOKEN, token_verification_enabled=False)

# Ops alert buttons ([Acknowledge] from the watchdog / digest, posted by traveldesk.ops.scheduler).
from traveldesk.ops import group as _ops_group  # noqa: E402
from traveldesk.ops import gst_report as _ops_gst  # noqa: E402
from traveldesk.ops import slack_actions as _ops_slack_actions  # noqa: E402

_ops_slack_actions.register(app)

HELP = (
    "*How to use /traveldesk*\n"
    "`/traveldesk Delhi to Mumbai Tuesday morning`\n"
    "`/traveldesk BLR to DEL 28 Nov after 6pm`\n"
    "Or just message me here, e.g. `Delhi to Mumbai Tuesday morning`.\n"
    "I search real fares, pick one by the company rule, and ask you to confirm before booking."
)
_HELP_WORDS = {"", "help", "hi", "hello", "hey", "?"}


# ---------------------------------------------------------------------------
# Slash command / direct message: start a request
# ---------------------------------------------------------------------------


@app.command("/traveldesk")
def traveldesk(ack, command, client):
    ack()  # Slack needs an answer within 3 seconds; the real work runs in a thread
    user = command["user_id"]
    text = (command.get("text") or "").strip()
    dm = client.conversations_open(users=user)["channel"]["id"]
    start_request(client, user, text, dm)


@app.event("message")
def on_message(event, client):
    """A message in the bot's DM is handled exactly like `/traveldesk <text>`.

    A thread reply under a search that stopped at need_info / no_match is a
    follow-up: the original request plus the reply is searched again.
    Everything else (channel messages, edits, deletions, the bot's own
    posts, other thread replies) is ignored or gets a hint."""
    if event.get("channel_type") != "im":
        return
    if event.get("subtype") or event.get("bot_id") or not event.get("user"):
        return  # edits, deletions, joins, and messages posted by bots (incl. this one)
    if event.get("thread_ts") and event.get("thread_ts") != event.get("ts"):
        reply = (event.get("text") or "").strip()
        row = db.by_thread(event["user"], event["thread_ts"])
        if reply and row and row["status"] in _FOLLOW_UP_STATUSES and _row_details(row).get("search_stopped"):
            start_request(client, event["user"], f"{row['request']} — {reply}", event["channel"])
            return
        client.chat_postMessage(
            channel=event["channel"],
            thread_ts=event["thread_ts"],
            text="To start a new request, send it as a new message (not a thread reply).",
        )
        return
    start_request(client, event["user"], (event.get("text") or "").strip(), event["channel"])


# Search outcomes a thread reply can answer. Only rows run_search stopped
# (details.search_stopped) qualify — booking-phase need_info has its own flow.
_FOLLOW_UP_STATUSES = ("need_info", "no_match")


def start_request(client, user, text, dm):
    """Shared by /traveldesk and DMs: checks, then search on a background thread."""
    if text.lower().strip(" !.") in _HELP_WORDS:
        client.chat_postMessage(channel=dm, text=HELP)
        return

    # `gst [month]`: GST summary + dashboard link (travel admins only: company tax data).
    first, _, rest = text.partition(" ")
    if first.lower() == "gst":
        if user not in config.TRAVEL_ADMINS:
            client.chat_postMessage(channel=dm, text="The GST summary is for travel admins only.")
            return
        try:
            reply = _ops_gst.for_slack(rest)
        except Exception:  # noqa: BLE001 — answer the admin rather than fail silently
            log.exception("gst summary failed")
            reply = "Couldn't build the GST summary right now — check the bot's log."
        client.chat_postMessage(channel=dm, text=reply)
        return

    # Several travellers (@mentions): the ops group flow (admins only; one Confirm for everyone).
    if _ops_group.is_group_request(text, user):
        _ops_group.start_from_bot(client, user, text, dm)
        return

    traveller = traveller_profile.load(user)
    if not traveller:
        client.chat_postMessage(
            channel=dm,
            text=f"You're not set up yet. Ask the admin to add your Slack ID `{user}` to travellers.json.",
        )
        return
    search_check = traveller_profile.validate(traveller, for_booking=False)
    if not search_check.ok:
        client.chat_postMessage(
            channel=dm,
            text=":warning: Your travellers.json entry needs fixing before I can search — "
            f"{search_check.summary()}. Ask the admin to update it.",
        )
        return
    # Sensitive fields never reach the AI provider.
    traveller = traveller_profile.public_view(traveller)

    # Duplicate-request protection: a near-identical request already in flight.
    dup = db.recent_pending_for(user, text)
    if dup:
        client.chat_postMessage(
            channel=dm,
            text=f":warning: You already have a request in flight for this (id {dup['id']}, status `{dup['status']}`). "
            "Wait for that one to finish or cancel it before trying again.",
        )
        return

    first = client.chat_postMessage(channel=dm, text=f":airplane: *{text}*\nSearching flights… (2–3 minutes)")
    threading.Thread(
        target=run_search,
        args=(client, dm, first["ts"], user, text, traveller),
        daemon=True,
    ).start()


def say(client, channel, thread_ts, text):
    client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text)


# ---------------------------------------------------------------------------
# Phase 1: search + pick, then ask the employee to confirm
# ---------------------------------------------------------------------------


def run_search(client, channel, thread_ts, user, text, traveller):
    row_id, _key = db.new_request(user, text, config.AI_PROVIDER, thread_ts=thread_ts)
    try:
        agent = agent_factory.get_agent()
        picked = _call_agent_pick(agent, text, traveller, row_id)
    except Exception as e:  # noqa: BLE001 — surfaced to user
        log.exception("pick failed for row %s", row_id)
        if _stopped(row_id):
            return
        db.update(row_id, status="failed", details={"error": str(e)})
        say(client, channel, thread_ts, f":x: Search failed: {e}")
        return
    if _stopped(row_id):
        return

    status = picked.get("status")
    if status != "picked":
        db.update(row_id, status=status or "failed", details={**picked, "search_stopped": True})
        hint = (
            "\n_Reply in this thread to add or correct details and I'll search again._"
            if status in _FOLLOW_UP_STATUSES
            else ""
        )
        say(
            client,
            channel,
            thread_ts,
            f":warning: Not booked ({status}). {picked.get('message', '')}\n{picked.get('summary', '')}{hint}",
        )
        return

    price = picked.get("price")
    currency = picked.get("currency") or config.CURRENCY

    db.update(
        row_id,
        status="awaiting_confirm",
        price=price,
        currency=currency,
        details=picked,
    )

    _post_confirmation_card(client, row_id, user, text, picked, channel, traveller)


def _call_agent_pick(agent, text, traveller, row_id):
    """Call agent.pick with booking_id when the provider supports it."""
    try:
        return agent.pick(text, traveller, booking_id=row_id)
    except TypeError:
        # Agents without a booking_id kwarg (test doubles).
        return agent.pick(text, traveller)


def _call_agent_confirm(agent, picked, traveller, row_id):
    try:
        return agent.confirm(picked, traveller, booking_id=row_id)
    except TypeError:
        return agent.confirm(picked, traveller)


def _call_agent_status(agent, booking_ref, row_id):
    try:
        return agent.booking_status(booking_ref, booking_id=row_id)
    except TypeError:
        return agent.booking_status(booking_ref)


def _stopped(row_id) -> bool:
    """True if the request was cancelled (e.g. from the web dashboard) while a
    background step was running: the step must stop without messaging or booking."""
    if db.is_cancelled(row_id):
        log.info("row %s was cancelled; stopping its background work", row_id)
        return True
    return False


def _post_confirmation_card(client, row_id, employee, request_text, picked, employee_dm, traveller):
    """Send one Block Kit card to the requesting employee with Confirm / Cancel."""
    blocks = _confirm_blocks(row_id, employee, request_text, picked, traveller)
    try:
        msg = client.chat_postMessage(
            channel=employee_dm,
            text=f"Confirm your travel booking: {picked.get('summary', '')}",
            blocks=blocks,
        )
        db.update(row_id, manager_message_ts=msg["ts"], manager_channel=employee_dm)
    except Exception as e:  # noqa: BLE001
        log.warning("could not post confirmation card to %s: %s", employee, e)


def _passport_warning(picked, employee=None) -> str | None:
    """Learned from need_info_log: warn before booking a fare known to ask for passports."""
    if not picked.get("domestic"):
        return None  # international already collects the passport
    if employee and letsfg_booking.passport_restriction(employee):
        return (
            ":warning: LetsFG currently requires passport details even for domestic "
            "flights. If yours aren't saved yet, you'll be asked for them after you confirm."
        )
    try:
        seen = db.passport_history(picked.get("airline"), picked.get("fare_type"))
    except Exception:  # noqa: BLE001
        return None
    name = picked.get("airline_name") or picked.get("airline")
    if seen == "fare":
        return (
            f":warning: {name} has asked for passport details on this fare type before, "
            "even within India. You may be asked for them."
        )
    if seen == "airline":
        return (
            f":warning: Some {name} fares have asked for passport details, even within "
            "India. You may be asked for them."
        )
    return None


_CURRENCY_SYMBOLS = {"INR": "₹", "USD": "$", "EUR": "€", "GBP": "£", "AED": "AED "}

# Search and booking are for one traveller (the requester).
TICKETS_PER_BOOKING = 1


def _money(amount, currency) -> str:
    """58238.0, "INR" → "₹58,238"; 99.5, "USD" → "$99.50"."""
    if amount is None:
        return "price n/a"
    try:
        value = float(amount)
    except (TypeError, ValueError):
        return f"{amount} {currency}"
    text = f"{value:,.0f}" if value == int(value) else f"{value:,.2f}"
    symbol = _CURRENCY_SYMBOLS.get(str(currency).upper())
    return f"{symbol}{text}" if symbol else f"{text} {currency}"


def _fare_rule_blocks(picked) -> list:
    """The fare's full rules (refund, change, no-show, bags, …) shown before Confirm."""
    rows = picked.get("fare_conditions")
    if not isinstance(rows, list) or not rows:
        text = "*Fare rules*\nThe seller didn't send fare rules for this flight. Check them on the offer page before you confirm."
    else:
        lines = [f"• *{r.get('label')}:* {r.get('value')}" for r in rows if isinstance(r, dict)]
        text = "*Fare rules*\n" + "\n".join(lines)
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": text[:3000]}},
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": "As given by the seller. The airline's full fare conditions apply.",
                }
            ],
        },
    ]


def _confirm_blocks(row_id, employee, request_text, picked, traveller):
    price = picked.get("price")
    currency = picked.get("currency") or config.CURRENCY
    tickets = TICKETS_PER_BOOKING
    per_ticket = _money(price, currency)
    total = _money(price * tickets if isinstance(price, (int, float)) else None, currency)
    price_line = total  # used by the confirm dialog
    traveller_line = (
        f"{traveller_profile.full_given_name(traveller)} {traveller.get('family_name', '')}".strip()
    )
    offer_link = picked.get("offer_link")
    value = json.dumps({"row": row_id})
    fields = [
        {"type": "mrkdwn", "text": f"*Traveller*\n{traveller_line or '—'}"},
        {"type": "mrkdwn", "text": f"*Tickets*\n{tickets}"},
        {"type": "mrkdwn", "text": f"*Price per ticket*\n{per_ticket}"},
        {"type": "mrkdwn", "text": f"*Total*\n*{total}*"},
    ]
    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "Confirm your booking"},
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f":airplane: *{picked.get('summary', '(no summary)')}*"},
        },
        {"type": "section", "fields": fields},
    ]
    blocks += _fare_rule_blocks(picked)
    asked = picked.get("passengers_requested")
    if isinstance(asked, int) and asked > tickets:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f":warning: You asked for *{asked} tickets*, but I can only book *{tickets}* "
                    f"(for {traveller_line or 'you'}). Each other traveller needs their own request "
                    "from their own Slack account.",
                },
            }
        )
    footer = [f"Request: _{request_text}_"]
    if offer_link:
        footer.append(f"<{offer_link}|View offer on LetsFG>")
    footer.append(f"Picked by `{picked.get('model') or picked.get('provider', '?')}`")
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": "  ·  ".join(footer)}]})
    if picked.get("notice"):
        blocks.insert(1, {"type": "section", "text": {"type": "mrkdwn", "text": picked["notice"]}})
    warning = _passport_warning(picked, employee)
    if warning:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": warning}]})
    ticket_word = "ticket" if tickets == 1 else "tickets"
    confirm_text = (
        f"This will book {tickets} {ticket_word} for {price_line} total. This is a real booking."
        if not config.DRY_RUN
        else f"DRY_RUN is on. This will search and pick only, no real booking ({tickets} {ticket_word}, {price_line})."
    )
    blocks.append(
        {
            "type": "actions",
            "block_id": f"traveldesk_decision_{row_id}",
            "elements": [
                {
                    "type": "button",
                    "style": "primary",
                    "text": {"type": "plain_text", "text": "Confirm & book"},
                    "action_id": "confirm_booking",
                    "value": value,
                    "confirm": {
                        "title": {"type": "plain_text", "text": "Confirm booking?"},
                        "text": {"type": "mrkdwn", "text": confirm_text},
                        "confirm": {"type": "plain_text", "text": "Confirm"},
                        "deny": {"type": "plain_text", "text": "Back"},
                    },
                },
                {
                    "type": "button",
                    "style": "danger",
                    "text": {"type": "plain_text", "text": "Cancel"},
                    "action_id": "cancel_booking",
                    "value": value,
                },
            ],
        }
    )
    return blocks


# ---------------------------------------------------------------------------
# Phase 2: employee clicks Confirm / Cancel
# ---------------------------------------------------------------------------


@app.action("confirm_booking")
def on_confirm(ack, body, client, action):
    ack()
    _handle_decision(body, client, action, "confirmed")


@app.action("cancel_booking")
def on_cancel(ack, body, client, action):
    ack()
    _handle_decision(body, client, action, "cancelled")


def _handle_decision(body, client, action, decision):
    actor = body["user"]["id"]
    try:
        payload = json.loads(action["value"])
        row_id = int(payload["row"])
    except (KeyError, ValueError, TypeError, json.JSONDecodeError) as e:
        log.warning("malformed action payload: %s", e)
        return

    row = db.get(row_id)
    if not row:
        log.warning("decision for unknown row %s", row_id)
        return

    # Requester-only: only the employee who filed the request may confirm /
    # cancel it. No manager / admin override.
    if actor != row["slack_user"]:
        try:
            ephemeral = body.get("channel", {}).get("id") or body.get("container", {}).get("channel_id")
            if ephemeral:
                client.chat_postEphemeral(
                    channel=ephemeral,
                    user=actor,
                    text=":no_entry: Only the requesting employee can confirm or cancel this booking.",
                )
        except Exception:  # noqa: BLE001 — silently swallowed; fixed when app.py is split
            pass
        log.warning("unauthorised %s attempt row=%s user=%s", decision, row_id, actor)
        return

    if row["status"] != "awaiting_confirm":
        _update_card_as_done(client, body, text=f"Already `{row['status']}`")
        return

    # Collect missing passenger details first; the decision is only claimed
    # once they are complete, so the card stays usable until then.
    if decision == "confirmed" and _request_passenger_details(client, row, body.get("trigger_id")):
        return

    if not db.claim_decision(row_id, actor, decision):
        # Lost the race with a duplicate click / Slack retry.
        latest = db.get(row_id) or {}
        _update_card_as_done(client, body, text=f"Already `{latest.get('status')}`")
        return

    _update_card_as_done(client, body, text=f"{decision.title()} by <@{actor}>")

    employee = row["slack_user"]
    picked = json.loads(row["details"] or "{}")
    traveller = traveller_profile.load(employee) or {}

    if decision == "cancelled":
        _dm(client, employee, f":no_entry: Cancelled your travel request `{row['request']}`.")
        return

    # Confirmed — start the booking on a background thread.
    threading.Thread(
        target=run_booking,
        args=(client, row_id, employee, picked, traveller),
        daemon=True,
    ).start()


# ---------------------------------------------------------------------------
# Passenger details (DOB, gender, passport) — collected in a private modal
# ---------------------------------------------------------------------------

_DATE_FIELDS = ("date_of_birth", "passport_expiry", "passport_issue")
_MAX_LEN = {
    "nationality": 2,
    "passport_country": 2,
    "passport_number": 20,
    "phone": 20,
    "given_name": 50,
    "middle_name": 50,
    "family_name": 50,
    "address_line1": 100,
    "address_city": 50,
    "address_postal": 10,
    "address_country": 2,
    "phone_country": 2,
}
_DOMESTIC_ID_LINE = "Carry the ID you selected (or use DigiYatra) at the airport."
_DOMESTIC_LABELS = {
    "given_name": "First name (exactly as on your ID)",
    "middle_name": "Middle name(s), if on your ID",
    "family_name": "Last name (exactly as on your ID)",
}


def _validate_for_trip(profile, picked):
    """Domestic India trips need the basic profile; others need the passport too."""
    return traveller_profile.validate(
        profile,
        for_booking=True,
        travel_date=picked.get("depart_date"),
        domestic=bool(picked.get("domestic")),
        last_flight_date=picked.get("last_flight_date"),
    )


def _request_passenger_details(client, row, trigger_id) -> bool:
    """Return True if booking must wait for passenger details (and ask for them)."""
    picked = json.loads(row["details"] or "{}")
    if picked.get("backend") != "rest":
        return False  # not a REST pick: letsfg_booking.book refuses it
    domestic = bool(picked.get("domestic"))
    profile = traveller_profile.load(row["slack_user"]) or {}
    res = _validate_for_trip(profile, picked)
    if res.ok:
        return False  # profile already complete → straight to booking
    employee = row["slack_user"]
    problems = res.problem_fields()
    admin_fields = [f for f in problems if f not in traveller_profile.MODAL_FIELDS]
    if admin_fields:
        _dm(
            client,
            employee,
            ":warning: Can't book yet — your travellers.json entry needs: "
            + ", ".join(traveller_profile.LABELS.get(f, f) for f in admin_fields)
            + ". Ask the admin to update it, then press Confirm again.",
        )
        return True
    if not secure_store.is_configured():
        log.warning("passenger details needed for row %s but secure storage is not configured", row["id"])
        _dm(
            client,
            employee,
            ":warning: Can't collect passport details: secure storage isn't configured "
            "(TRAVELDESK_DATA_KEY). Ask the admin.",
        )
        return True
    if not trigger_id:
        _dm(client, employee, "Passenger details are needed — press Confirm again to enter them.")
        return True
    fields = traveller_profile.modal_fields(problems, domestic=domestic)
    try:
        client.views_open(
            trigger_id=trigger_id,
            view=_passenger_modal(row["id"], fields, res.invalid, profile, domestic=domestic),
        )
    except Exception as e:  # noqa: BLE001
        log.warning("could not open passenger-details modal for row %s: %s", row["id"], type(e).__name__)
        _dm(client, employee, ":warning: Couldn't open the passenger-details form. Try Confirm again.")
    return True


def _option(value, text):
    return {"text": {"type": "plain_text", "text": text}, "value": value}


# F / M / X: the sex marker printed on the passport (ICAO Doc 9303).
_GENDER_OPTIONS = [_option("m", "Male (M)"), _option("f", "Female (F)"), _option("x", "Unspecified (X)")]
_ID_TYPE_OPTIONS = [_option(k, v) for k, v in traveller_profile.ID_TYPES.items()]


def _passenger_modal(
    row_id,
    fields,
    invalid,
    profile,
    *,
    domestic,
    callback_id="passenger_details",
    intro=None,
    required=(),
    extra_blocks=(),
):
    """Domestic: basic details + ID type only. International: passport form.

    Saved values are pre-filled, except the passport number, which is never
    sent back to Slack — only its last 3 characters are shown. Fields in
    `required` are always mandatory inputs."""
    profile = profile or {}
    if intro is not None:
        pass
    elif domestic:
        intro = (
            ":lock: Enter your name exactly as on the ID you'll carry. "
            "No ID number is needed for flights within India."
        )
    else:
        intro = (
            ":lock: Enter details exactly as on your passport. Your passport must be valid "
            f"for {traveller_profile.PASSPORT_VALIDITY_MONTHS} months after your last flight. "
            "Details are stored encrypted, used only to book your own flights, and never "
            "shown in Slack messages."
        )
    blocks = [{"type": "context", "elements": [{"type": "mrkdwn", "text": intro}]}]
    for f in fields:
        saved = profile.get(f)
        saved = None if saved in (None, "") else str(saved)
        optional = False
        hint = None
        if f == "gender":
            element = {"type": "static_select", "action_id": "value", "options": _GENDER_OPTIONS}
            opt = next((o for o in _GENDER_OPTIONS if saved and o["value"] == str(saved).lower()[:1]), None)
            if opt:
                element["initial_option"] = opt
        elif f == "id_type":
            element = {
                "type": "static_select",
                "action_id": "value",
                "options": _ID_TYPE_OPTIONS,
                "placeholder": {"type": "plain_text", "text": "Choose an ID"},
            }
            opt = next((o for o in _ID_TYPE_OPTIONS if o["value"] == saved), None)
            if opt:
                element["initial_option"] = opt
            optional = True
        elif f in _DATE_FIELDS:
            element = {"type": "datepicker", "action_id": "value"}
            if saved:
                try:
                    element["initial_date"] = date.fromisoformat(saved).isoformat()
                except ValueError:
                    pass
        else:
            element = {"type": "plain_text_input", "action_id": "value", "max_length": _MAX_LEN.get(f, 50)}
            if f == "passport_number":
                if saved and f not in invalid:
                    # Never echo the number; keep it unless a new one is typed.
                    optional = True
                    hint = f"On file: {traveller_profile.mask_passport(saved)}. Leave blank to keep it."
            elif saved:
                element["initial_value"] = saved[: _MAX_LEN.get(f, 50)]
        if f in ("passport_issue", "middle_name"):
            optional = True  # optional unless LetsFG asked for it
        if f == "middle_name":
            hint = (
                "Leave blank if your passport has none. It's sent with your given name, as airlines expect."
            )

        if f in required:
            optional, hint = False, None
        label = (_DOMESTIC_LABELS.get(f) if domestic else None) or traveller_profile.LABELS.get(f, f)
        block = {
            "type": "input",
            "block_id": f,
            "element": element,
            "label": {"type": "plain_text", "text": label[:2000]},
        }
        if optional:
            block["optional"] = True
        if f in invalid:
            hint = f"Saved value rejected: {invalid[f]}"
        if hint:
            block["hint"] = {"type": "plain_text", "text": hint[:2000]}
        blocks.append(block)
    blocks += list(extra_blocks)
    return {
        "type": "modal",
        "callback_id": callback_id,
        "private_metadata": json.dumps({"row": row_id, "fields": list(fields)}),
        "title": {"type": "plain_text", "text": "Passenger details"},
        "submit": {"type": "plain_text", "text": "Save & book" if not config.DRY_RUN else "Save & continue"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": blocks,
    }


def _submitted_values(view) -> dict:
    out = {}
    for block_id, actions in ((view.get("state") or {}).get("values") or {}).items():
        a = actions.get("value") or {}
        if a.get("type") == "datepicker":
            v = a.get("selected_date")
        elif a.get("type") == "static_select":
            v = (a.get("selected_option") or {}).get("value")
        else:
            v = a.get("value")
        if block_id in traveller_profile.MODAL_FIELDS:
            out[block_id] = v
    return out


@app.view("passenger_details")
def on_passenger_details(ack, body, client, view):
    _handle_passenger_details(ack, body, client, view)


def _handle_passenger_details(ack, body, client, view):
    actor = body["user"]["id"]
    try:
        meta = json.loads(view.get("private_metadata") or "{}")
        row_id = int(meta["row"])
    except (KeyError, ValueError, TypeError):
        ack()
        return
    row = db.get(row_id)
    if not row or row["slack_user"] != actor:
        ack()
        log.warning("unauthorised passenger-details submission row=%s user=%s", row_id, actor)
        return

    submitted = _submitted_values(view)
    picked = json.loads(row["details"] or "{}")
    shown = set(meta.get("fields") or submitted)
    merged = {**(traveller_profile.load(actor) or {}), **{k: v for k, v in submitted.items() if v}}
    res = _validate_for_trip(merged, picked)
    # Inline errors on the fields this modal showed (e.g. passport expiring
    # less than 6 months after the last flight).
    errors = {f: res.invalid.get(f, "Required") for f in res.problem_fields() if f in shown}
    if errors:
        ack(response_action="errors", errors=errors)
        return
    to_save = {f: res.cleaned[f] for f, v in submitted.items() if v and f in res.cleaned}
    try:
        secure_store.save(actor, to_save)
    except secure_store.SecureStoreUnavailable:
        first = next(iter(shown), None)
        ack(
            response_action="errors",
            errors={first: "Secure storage is not configured — ask the admin."} if first else {},
        )
        return
    ack()
    saved_msg = ":lock: Passenger details saved securely."
    if "passport_number" in res.cleaned and not picked.get("domestic"):
        saved_msg += f" Passport on file: {traveller_profile.mask_passport(res.cleaned['passport_number'])}."
    _dm(client, actor, saved_msg)
    if not res.ok:  # admin-managed fields still missing
        _dm(client, actor, ":warning: Still can't book: " + res.summary())
        return

    if row["status"] != "awaiting_confirm" or not db.claim_decision(row_id, actor, "confirmed"):
        latest = db.get(row_id) or {}
        _dm(client, actor, f"This booking is already `{latest.get('status')}`.")
        return
    if row.get("manager_channel") and row.get("manager_message_ts"):
        try:
            client.chat_update(
                channel=row["manager_channel"],
                ts=row["manager_message_ts"],
                text=f"Booking confirmed: {picked.get('summary', '')}",
                blocks=[
                    {"type": "section", "text": {"type": "mrkdwn", "text": f"*{picked.get('summary', '')}*"}},
                    {
                        "type": "context",
                        "elements": [{"type": "mrkdwn", "text": f":lock: Confirmed by <@{actor}>"}],
                    },
                ],
            )
        except Exception as e:  # noqa: BLE001
            log.warning("chat_update failed: %s", type(e).__name__)
    threading.Thread(
        target=run_booking,
        args=(client, row_id, actor, picked, traveller_profile.load(actor) or {}),
        daemon=True,
    ).start()


def _update_card_as_done(client, body, text):
    """Replace the Confirm/Cancel buttons with a plain status line."""
    msg = body.get("message") or {}
    blocks = list(msg.get("blocks") or [])
    blocks = [b for b in blocks if b.get("type") != "actions"]
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": f":lock: {text}"}]})
    channel = body.get("channel", {}).get("id") or body.get("container", {}).get("channel_id")
    ts = msg.get("ts") or body.get("container", {}).get("message_ts")
    if channel and ts:
        try:
            client.chat_update(channel=channel, ts=ts, text=f"Booking {text}", blocks=blocks)
        except Exception as e:  # noqa: BLE001 — Slack API failure must not break the handler
            log.warning("chat_update failed: %s", e)


def _dm(client, user, text, blocks=None) -> bool:
    try:
        channel = client.conversations_open(users=user)["channel"]["id"]
        if blocks:
            client.chat_postMessage(channel=channel, text=text, blocks=blocks)
        else:
            client.chat_postMessage(channel=channel, text=text)
        return True
    except Exception as e:  # noqa: BLE001 — Slack API failure is reported via the return value
        log.warning("DM to %s failed: %s", user, e)
        return False


# ---------------------------------------------------------------------------
# Phase 3: book + poll status
# ---------------------------------------------------------------------------


def run_booking(client, row_id, employee, picked, traveller):
    employee_dm = None
    try:
        employee_dm = client.conversations_open(users=employee)["channel"]["id"]
    except Exception as e:  # noqa: BLE001 — Slack API failure must not stop the booking thread
        log.warning("open DM to employee %s failed: %s", employee, e)

    agent = agent_factory.get_agent()
    try:
        result = _call_agent_confirm(agent, picked, traveller, row_id)
    except Exception as e:  # noqa: BLE001
        log.exception("confirm failed for row %s", row_id)
        db.update(row_id, status="failed", details={"error": str(e)})
        _notify(client, employee_dm, f":x: Booking failed to start: {e}")
        return
    _handle_book_result(client, row_id, employee, employee_dm, picked, result)


def _handle_book_result(client, row_id, employee, employee_dm, picked, result, *, retry=False):
    """Store and report a LetsFG book result. `retry` = this was the retry
    after the traveller supplied documents; a failure now goes to the admin."""
    if _stopped(row_id):
        _book_result_after_cancel(client, row_id, employee, picked, result)
        return
    status = result.get("status", "failed")
    if status == "need_info" and ("missing_fields" in result or result.get("escalate")):
        _handle_need_info(client, row_id, employee, employee_dm, picked, result, retry=retry)
        return
    if status == "payment_link":
        # Card declined (or none saved) and LetsFG offers a secure payment page:
        # no "Booking started" — go straight to "payment needs approval".
        _start_payment_link(client, row_id, employee, employee_dm, picked, result)
        return
    if status == "payment_blocked":
        _payment_blocked(client, row_id, employee, employee_dm, picked, result)
        return
    if status == "awaiting_payment":
        _notify(
            client, employee_dm, ":hourglass: This booking is already waiting for payment on its secure link."
        )
        return
    if retry and (result.get("offer_expired") or result.get("price_changed")):
        db.update(row_id, status="expired", details={**picked, **result})
        threading.Thread(
            target=reprice_and_reconfirm,
            args=(client, row_id, employee, employee_dm, picked, result),
            daemon=True,
        ).start()
        return

    db.update(
        row_id,
        status=status,
        booking_ref=result.get("booking_ref"),
        details={**picked, **result},
        payment_status="awaiting_payment"
        if result.get("approve_url")
        else result.get("payment_status", "unpaid"),
    )
    if result.get("booking_ref") and status not in _NOTHING_BOOKED + ("picked",):
        # LetsFG accepted: pending booking + pending debit now (a pay/approve page: no debit yet).
        _track_in_ops(row_id, "awaiting_payment" if result.get("approve_url") else "pending", picked)

    price = f"{result.get('price', '')} {result.get('currency', '')}".strip()
    summary = result.get("summary", "") or picked.get("summary", "")
    msg = result.get("message", "")

    domestic = bool(picked.get("domestic"))
    id_line = f"\n:identification_card: {_DOMESTIC_ID_LINE}" if domestic else ""

    if status == "picked":  # DRY_RUN
        _notify(
            client,
            employee_dm,
            f":test_tube: *Dry run* — would book: {summary} · {price}{id_line}\n(DRY_RUN=true in .env)",
        )
        return
    if status in ("no_match", "need_info", "setup_required", "failed"):
        if retry:
            told = _escalate(
                client,
                row_id,
                employee,
                picked,
                f"Booking still failed after the traveller added passport details: {status} — {msg}",
            )
            msg += " " + told
        _notify(client, employee_dm, f":warning: Not booked ({status}). {msg}\n{summary}")
        return

    lines = [f":white_check_mark: Booking started: {summary} · {price}"]
    if result.get("approve_url"):
        lines.append(f":point_right: Secure payment link: {result['approve_url']}")
        lines.append("_Payment is not complete until LetsFG confirms the ticket._")
    elif status == "awaiting_approval":
        lines.append(":point_right: Approve & pay on letsfg.co (check your LetsFG account/email).")
    _notify(client, employee_dm, "\n".join(lines))

    if result.get("booking_ref"):
        watch_booking(client, row_id, employee_dm, result["booking_ref"], domestic=domestic)


_NOTHING_BOOKED = ("cancelled", "failed", "no_match", "need_info", "setup_required", "payment_blocked")


def _book_result_after_cancel(client, row_id, employee, picked, result):
    """The row was cancelled while LetsFG was being called. If LetsFG still
    started a booking, keep its ref and tell the admin; nobody else is told."""
    ref, status = result.get("booking_ref"), result.get("status")
    if not ref or status in _NOTHING_BOOKED:
        return
    db.update(row_id, booking_ref=ref, details={**picked, **result})
    _escalate(
        client,
        row_id,
        employee,
        picked,
        f"Cancelled from the dashboard while LetsFG was booking it (LetsFG booking {ref}, `{status}`). "
        "Check it with LetsFG and cancel it there if needed.",
    )


def _track_in_ops(row_id, state: str, pick: dict | None = None, reason: str = "") -> None:
    """Keep the ops ledger in step before the ticket (pending / awaiting_payment / failed / cancelled).
    Never blocks the booking flow."""
    try:
        from traveldesk.ops import legacy as ops_legacy

        ops_legacy.track_legacy_id(row_id, state, pick=pick, reason=reason)
    except Exception:  # noqa: BLE001 — ops bookkeeping must not break the booking
        log.exception("ops tracking (%s) failed for booking %s", state, row_id)


def _mirror_to_ops(row_id, pick: dict) -> None:
    """Record the ticket in the ops ledger tables. Never blocks the ticket message."""
    try:
        from traveldesk.ops import legacy as ops_legacy

        ops_legacy.mirror_legacy_id(row_id, pick=pick)
    except Exception:  # noqa: BLE001 — ops bookkeeping must not break ticket delivery
        log.exception("ops mirror failed for booking %s", row_id)


def _notify(client, employee_dm, text):
    if employee_dm:
        try:
            client.chat_postMessage(channel=employee_dm, text=text)
        except Exception as e:  # noqa: BLE001 — Slack API failure must not stop the booking thread
            log.warning("notify employee failed: %s", e)


def watch_booking(client, row_id, employee_dm, booking_ref, domestic=False):
    agent = agent_factory.get_agent()
    deadline = time.time() + config.POLL_MAX_MINUTES * 60
    last = None
    while time.time() < deadline:
        time.sleep(config.POLL_SECONDS)
        try:
            st = _call_agent_status(agent, booking_ref, row_id)
        except Exception as e:  # noqa: BLE001 — transient status failure: keep polling
            log.warning("status check failed for %s: %s", booking_ref, e)
            continue
        if st.get("transient"):
            continue
        if st.get("status") == "payment_link":
            log.info("booking %s → payment declined, LetsFG offers a pay link", booking_ref)
            row = db.get(row_id) or {}
            _start_payment_link(client, row_id, row.get("slack_user"), employee_dm, _row_details(row), st)
            return
        state = st.get("status")
        if state != last:
            log.info("booking %s → %s", booking_ref, state)
            last = state
            if state == "awaiting_approval" and st.get("approve_url"):
                _notify(
                    client,
                    employee_dm,
                    f":point_right: LetsFG needs payment approval: {st['approve_url']}\n"
                    "_Payment is not complete until LetsFG confirms the ticket._",
                )
        if state == "completed" and not st.get("pnr"):
            state = "needs_attention"
            st = {
                **st,
                "status": state,
                "message": "LetsFG reported completed without a PNR — check with LetsFG.",
            }
        if state == "completed":
            charged = st.get("charged_amount")
            if charged is None:
                charged = st.get("total")
            before = db.get(row_id) or {}
            db.update(
                row_id,
                status="completed",
                pnr=st.get("pnr"),
                payment_status="paid",
                charged_amount=charged,
                details=st,
            )
            _mirror_to_ops(row_id, _row_details(before))
            text = (
                f":ticket: *Ticket issued!* PNR: *{st.get('pnr')}*\nTotal: {charged} {st.get('currency', '')}"
            )
            if st.get("ticket_url"):
                text += f"\nTicket: {st['ticket_url']}"
            if domestic:
                text += f"\n:identification_card: {_DOMESTIC_ID_LINE}"
            if before.get("pay_url"):
                _post_ticket_in_thread(client, before, employee_dm, text)
            else:
                _notify(client, employee_dm, text)
            return
        if state in ("failed", "approval_expired", "not_started", "needs_attention", "cancelled"):
            db.update(
                row_id, status=state, payment_status="failed" if state == "failed" else "unpaid", details=st
            )
            if state != "needs_attention":  # needs_attention may still be charged: leave the debit pending
                _track_in_ops(row_id, "cancelled" if state == "cancelled" else "failed", reason=state)
            _notify(client, employee_dm, f":warning: Booking {state}. {st.get('message', '')}")
            return
    db.update(row_id, status="timeout")
    _notify(
        client,
        employee_dm,
        ":hourglass: Still not ticketed after the waiting time. Check your LetsFG email/account.",
    )


# ---------------------------------------------------------------------------
# Pay by link: the card was declined (or none is saved) and LetsFG offers a
# secure payment page. Paying it starts the booking by itself, so the bot
# never calls /flights/book again for the request; it only polls.
# ---------------------------------------------------------------------------

_PAY_WAYS = "any card, Apple Pay, Google Pay or Revolut Pay"


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _parse_utc(value) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _local(dt: datetime) -> str:
    return dt.astimezone().strftime("%d %b %H:%M")


def _open_dm(client, user):
    try:
        return client.conversations_open(users=user)["channel"]["id"]
    except Exception as e:  # noqa: BLE001 — Slack API failure must not stop the payment thread
        log.warning("open DM to %s failed: %s", user, e)
        return None


def _approvers(row) -> list:
    saved = [u for u in str(row.get("pay_approvers") or "").split(",") if u]
    return saved or [row.get("slack_user")]


def _amount(details) -> str:
    amount = details.get("pay_amount") or details.get("price") or ""
    currency = details.get("pay_currency") or details.get("currency") or ""
    return f"{amount} {currency}".strip()


def _pay_blocks(text, pay_url):
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "style": "primary",
                    "action_id": "pay_now",
                    "text": {"type": "plain_text", "text": "Pay now"},
                    "url": pay_url,
                }
            ],
        },
    ]


def _search_again_blocks(text, row_id):
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": "search_again",
                    "text": {"type": "plain_text", "text": "Search again"},
                    "value": json.dumps({"row": row_id}),
                }
            ],
        },
    ]


def _start_payment_link(client, row_id, employee, employee_dm, picked, result):
    """Record the pay link, send "Pay now" to the approver(s), then follow it."""
    if _stopped(row_id):
        return
    pay_url, new_ref = result.get("pay_url"), result.get("booking_ref")
    summary = picked.get("summary", "")
    if not pay_url or not new_ref:
        db.update(row_id, status="failed", payment_status="failed", details={**picked, **result})
        _notify(
            client,
            employee_dm,
            f":warning: Not booked: the payment was declined. {result.get('message', '')}\n{summary}",
        )
        return
    now = _utcnow()
    valid_until = result.get("pay_valid_until")
    expires = (
        datetime.fromtimestamp(float(valid_until), UTC)
        if valid_until
        else now + timedelta(minutes=config.PAY_LINK_TIMEOUT_MINUTES)
    )
    approvers = list(config.PAYMENT_APPROVERS) or [employee]
    keep = {k: v for k, v in result.items() if k not in ("status", "pay_url")}
    db.update(
        row_id,
        status="awaiting_payment",
        booking_ref=new_ref,
        payment_status="awaiting_payment",
        pay_url=pay_url,
        declined_booking_ref=result.get("declined_booking_ref"),
        pay_started_at=now.isoformat(),
        pay_expires_at=expires.isoformat(),
        pay_reminded_at=None,
        pay_approvers=",".join(approvers),
        details={**picked, **keep},
    )
    _track_in_ops(row_id, "awaiting_payment", picked)
    log.warning(
        "payment needs approval row=%s booking=%s declined=%s kind=%s reason=%s expires=%s (%s) message=%s",
        row_id,
        new_ref,
        result.get("declined_booking_ref"),
        result.get("payment_kind"),
        result.get("decline_reason"),
        expires.isoformat(),
        "LetsFG valid_until" if valid_until else f"fallback {config.PAY_LINK_TIMEOUT_MINUTES} min",
        letsfg_rest._scrub(result.get("message", ""), {}),
    )
    declined = result.get("payment_kind") != "payment_required"
    why = " ".join(p for p in (result.get("decline_message"), result.get("decline_advice")) if p)
    text = (
        f":credit_card: *Payment needs approval* — request #{row_id} for <@{employee}>\n"
        f"*Flight:* {summary}\n*Amount:* {_amount({**picked, **result})}\n"
        + (f"*Card declined:* {why}\n" if declined and why else "")
        + f"Pay on the secure page ({_PAY_WAYS}); the booking then starts by itself. "
        f"Link valid until {_local(expires)}.\n_Nothing is booked or charged until it's paid._"
    )
    for user in approvers:
        _dm(client, user, text, _pay_blocks(text, pay_url))
    if approvers != [employee]:
        who = ", ".join(f"<@{u}>" for u in approvers)
        _notify(
            client,
            employee_dm,
            f":hourglass: *Waiting for payment approval* for *{summary}*. "
            + ("The card on file was declined, so " if declined else "")
            + f"{who} has the secure payment link. Nothing is booked or charged yet; "
            "I'll tell you as soon as it's paid.",
        )
    watch_payment(client, row_id)


def watch_payment(client, row_id):
    """Poll the pay link's booking until it's paid, closed or expired.

    Everything comes from the DB row, so this resumes correctly after a
    restart. LetsFG's status wins: the expiry time only ends the wait while
    LetsFG still says awaiting_payment."""
    agent = agent_factory.get_agent()
    row = db.get(row_id) or {}
    employee = row.get("slack_user")
    employee_dm = _open_dm(client, employee)
    while True:
        row = db.get(row_id) or {}
        # Cancelled from the dashboard: the link can't be withdrawn at LetsFG, so
        # keep an eye on it until it expires in case someone pays it anyway.
        cancelled = row.get("status") == "cancelled" and bool(row.get("pay_url"))
        if row.get("status") != "awaiting_payment" and not cancelled:
            return
        details = _row_details(row)
        now = _utcnow()
        started = _parse_utc(row.get("pay_started_at")) or now
        expires = _parse_utc(row.get("pay_expires_at")) or started + timedelta(
            minutes=config.PAY_LINK_TIMEOUT_MINUTES
        )
        if now >= expires:
            if cancelled:
                return
            _close_payment(
                client, row, employee_dm, "expired", "The payment link expired before it was paid."
            )
            return
        if not cancelled and not row.get("pay_reminded_at") and now >= started + (expires - started) / 2:
            db.update(row_id, pay_reminded_at=now.isoformat())
            text = (
                f":bell: *Reminder:* payment for request #{row_id} (<@{employee}>) is still waiting.\n"
                f"*Flight:* {details.get('summary', '')} · {_amount(details)}\n"
                f"Link valid until {_local(expires)}."
            )
            for user in _approvers(row):
                _dm(client, user, text, _pay_blocks(text, row["pay_url"]))
        time.sleep(config.POLL_SECONDS)
        try:
            st = _call_agent_status(agent, row["booking_ref"], row_id)
        except Exception as e:  # noqa: BLE001 — transient status failure: keep polling
            log.warning("pay-link status check failed for %s: %s", row["booking_ref"], e)
            continue
        state = st.get("status")
        if st.get("transient") or state == "awaiting_payment":
            continue
        log.info("pay-link booking %s → %s", row["booking_ref"], state)
        if cancelled or db.is_cancelled(row_id):
            _cancelled_pay_link_closed(client, db.get(row_id) or row, st)
            return
        if state in ("failed", "payment_link", "approval_expired", "expired", "not_started", "cancelled"):
            reason = st.get("message") or st.get("failure_reason") or "LetsFG closed the payment link."
            expired = state in ("approval_expired", "expired") or "expir" in reason.lower()
            _close_payment(client, row, employee_dm, "expired" if expired else "failed", reason)
            return
        if state == "needs_attention":
            db.update(row_id, status="needs_attention", details={**details, **st})
            msg = (
                f":warning: Request #{row_id} ({details.get('summary', '')}) needs attention at "
                f"LetsFG: {st.get('message', '')}"
            )
            for user in dict.fromkeys([*_approvers(row), employee]):
                _dm(client, user, msg)
            return
        # Paid: the booking is under way (or already done).
        db.update(row_id, status="booking_in_progress", payment_status="held")
        _track_in_ops(row_id, "pending", details)  # charged now: pending debit
        paid = (
            f":white_check_mark: Payment received for *{details.get('summary', '')}*. "
            "LetsFG is booking it now (usually 4-11 minutes)."
        )
        for user in _approvers(row):
            if user != employee:
                _dm(client, user, paid)
        _notify(client, employee_dm, paid)
        watch_booking(client, row_id, employee_dm, row["booking_ref"], domestic=bool(details.get("domestic")))
        return


def _cancelled_pay_link_closed(client, row, st):
    """A cancelled request's pay link left awaiting_payment. Closed: nothing to
    do. Paid: LetsFG is booking a cancelled trip, so the admin must act."""
    state = st.get("status")
    if state in ("failed", "payment_link", "approval_expired", "expired", "not_started", "cancelled"):
        return
    row_id, employee = row["id"], row.get("slack_user")
    details = _row_details(row)
    db.update(row_id, payment_status="held", details={**details, "paid_after_cancel": st})
    reason = (
        f"Cancelled from the dashboard, but its payment link was paid afterwards; LetsFG reports "
        f"`{state}` for booking {row.get('booking_ref')}. Cancel or refund it with LetsFG."
    )
    _escalate(client, row_id, employee, details, reason)
    for user in _approvers(row):
        if user not in config.TRAVEL_ADMINS:
            _dm(client, user, f":rotating_light: Request #{row_id} (<@{employee}>): {reason}")


def _close_payment(client, row, employee_dm, status, reason):
    """LetsFG closed the link or it expired: say so and offer a new search."""
    row_id = row["id"]
    details = _row_details(row)
    db.update(row_id, status=status, payment_status="unpaid")
    _track_in_ops(row_id, "failed", reason=f"pay link {status}")  # nothing was charged: no debit
    log.warning(
        "pay link closed row=%s booking=%s → %s: %s",
        row_id,
        row.get("booking_ref"),
        status,
        letsfg_rest._scrub(reason, {}),
    )
    word = "expired" if status == "expired" else "was not completed"
    text = (
        f":hourglass: The payment link for *{details.get('summary', '')}* {word}. "
        f"Nothing was booked or charged. {reason}"
    )
    employee = row.get("slack_user")
    for user in _approvers(row):
        if user != employee:
            _dm(client, user, f"Request #{row_id} (<@{employee}>): {text}")
    blocks = _search_again_blocks(text + "\nWant me to look again?", row_id)
    if employee_dm:
        try:
            client.chat_postMessage(channel=employee_dm, text=text, blocks=blocks)
            return
        except Exception as e:  # noqa: BLE001 — fall back to a plain DM
            log.warning("could not post search-again for row %s: %s", row_id, type(e).__name__)
    _dm(client, employee, text, blocks)


def _payment_blocked(client, row_id, employee, employee_dm, picked, result):
    """409 too_many_attempts: LetsFG won't retry this booking attempt's payment."""
    summary = picked.get("summary", "")
    db.update(row_id, status="payment_blocked", payment_status="failed", details={**picked, **result})
    log.warning("payment blocked row=%s: %s", row_id, letsfg_rest._scrub(result.get("message", ""), {}))
    admin = (
        f":no_entry: *Payment blocked by LetsFG* — request #{row_id} for <@{employee}>\n"
        f"*Flight:* {summary}\n"
        "The card was declined 5 times for this booking attempt (LetsFG counts per booking "
        "attempt), so LetsFG won't try it again. Nothing was charged.\n"
        "*Next:* connect another payment method on the LetsFG account; the traveller can then "
        "search again.\n"
        f"_LetsFG:_ {result.get('message', '')}"
    )
    approvers = list(config.PAYMENT_APPROVERS) or [employee]
    for user in approvers:
        _dm(client, user, admin)
    if approvers == [employee]:
        return
    text = (
        f":warning: Couldn't take payment for *{summary}*. Nothing was booked or charged. "
        "The payment approver has been told."
    )
    blocks = _search_again_blocks(text, row_id)
    if employee_dm:
        try:
            client.chat_postMessage(channel=employee_dm, text=text, blocks=blocks)
            return
        except Exception as e:  # noqa: BLE001 — fall back to a plain DM
            log.warning("could not tell traveller about blocked payment row %s: %s", row_id, type(e).__name__)
    _dm(client, employee, text, blocks)


def _post_ticket_in_thread(client, row, employee_dm, text):
    """Ticket issued for a pay-link booking: reply under the original confirmation card."""
    details = _row_details(row)
    full = f"{text}\n*Flight:* {details.get('summary', '')}"
    channel, ts = row.get("manager_channel"), row.get("manager_message_ts")
    posted = False
    if channel and ts:
        try:
            client.chat_postMessage(channel=channel, thread_ts=ts, text=full)
            posted = True
        except Exception as e:  # noqa: BLE001 — fall back to a plain DM
            log.warning("thread reply failed for row %s: %s", row["id"], type(e).__name__)
    if not posted:
        _notify(client, employee_dm, full)
    employee = row.get("slack_user")
    for user in _approvers(row):
        if user != employee:
            _dm(client, user, f"Request #{row['id']} (<@{employee}>): {full}")


@app.action("pay_now")
def on_pay_now(ack):
    ack()  # a link button; Slack still sends the click to the app


@app.action("search_again")
def on_search_again(ack, body, client, action):
    ack()
    _handle_search_again(body, client, action)


def _handle_search_again(body, client, action):
    actor = body["user"]["id"]
    try:
        row_id = int(json.loads(action["value"])["row"])
    except (KeyError, ValueError, TypeError):
        return
    row = db.get(row_id)
    if not row or row["slack_user"] != actor:
        log.warning("unauthorised search_again row=%s user=%s", row_id, actor)
        return
    if not db.claim_status(row_id, ("expired", "failed", "payment_blocked"), "replaced"):
        return  # already searched again (double click)
    _update_card_as_done(client, body, "Searching again…")
    dm = (body.get("channel") or {}).get("id") or _open_dm(client, actor)
    start_request(client, actor, row["request"], dm)


# ---------------------------------------------------------------------------
# need_info: LetsFG wants more passenger fields (e.g. passport on a domestic fare)
# ---------------------------------------------------------------------------

_DOCS_INTRO_DOMESTIC = (
    "This fare requires passport details even for a domestic flight. Saved securely for future bookings."
)
_DOCS_INTRO_INTL = "This fare requires these passport details. Saved securely for future bookings."
_ALT_LABEL = (
    ":grey_question: *Needs confirmation:* not yet confirmed bookable without a passport. "
    "LetsFG only tells us when booking; if it asks for passport details, nothing is "
    "charged and you'll be told."
)
_PASSPORT_EVERYWHERE = (
    "LetsFG currently requires passport details even for domestic flights "
    "within India, on every airline, so another flight won't avoid it."
)


def _passport_everywhere(employee, picked=None, result=None) -> bool:
    """Has LetsFG shown it needs a passport on every booking for this traveller?"""
    if (result or {}).get("passport_required_everywhere") or (picked or {}).get(
        "passport_required_everywhere"
    ):
        return True
    return bool(letsfg_booking.passport_restriction(employee))


def _no_passport_button(row_id, restricted):
    if restricted:
        return {
            "type": "button",
            "action_id": "request_manual_booking",
            "value": json.dumps({"row": row_id}),
            "text": {"type": "plain_text", "text": "No passport — ask the travel admin"},
        }
    return {
        "type": "button",
        "action_id": "no_passport",
        "value": json.dumps({"row": row_id}),
        "text": {"type": "plain_text", "text": "I don't have a passport"},
    }


def _row_details(row) -> dict:
    try:
        d = json.loads((row or {}).get("details") or "{}")
    except (TypeError, ValueError):
        d = {}
    return d if isinstance(d, dict) else {}


def _labels(fields) -> str:
    return ", ".join(traveller_profile.LABELS.get(f, f) for f in fields)


def _escalate(client, row_id, employee, picked, reason) -> str:
    """Tell the travel admin(s). Field names only — never document values.
    Returns a sentence for the traveller saying who was told."""
    text = (
        f":rotating_light: TravelDesk request #{row_id} for <@{employee}> needs a human.\n"
        f"*Flight:* {picked.get('summary', '?')} "
        f"({picked.get('airline_name') or picked.get('airline') or '?'}"
        f"{', fare ' + picked['fare_type'] if picked.get('fare_type') else ''})\n"
        f"*Problem:* {reason}"
    )
    delivered = [a for a in config.TRAVEL_ADMINS if _dm(client, a, text)]
    log.warning("escalated row %s to %d admin(s): %s", row_id, len(delivered), reason)
    if delivered:
        return "The travel admin has been told."
    return (
        "No travel admin is set up to be notified (TRAVEL_ADMIN_SLACK_IDS), so please contact them directly."
    )


def _handle_need_info(client, row_id, employee, employee_dm, picked, result, *, retry):
    missing = list(result.get("missing_fields") or [])
    required = sorted(
        set(picked.get("required_fields") or []) | set(result.get("required_fields") or []) | set(missing)
    )
    details = {
        **picked,
        "required_fields": required,
        "missing_fields": missing,
        # The next call needs a new idempotency key: nothing was booked.
        "book_attempt": int(result.get("book_attempt") or 0) + 1,
        "need_info_message": result.get("message", ""),
    }
    restricted = bool(picked.get("domestic")) and _passport_everywhere(employee, picked, result)
    if restricted:
        details["passport_required_everywhere"] = True
    details.pop("notice", None)
    uncollectable = [f for f in missing if f not in traveller_profile.MODAL_FIELDS]
    if retry or result.get("escalate") or not missing or uncollectable:
        db.update(row_id, status="needs_attention", details=details)
        asked = ", ".join(missing) or "unspecified fields"
        why = (
            "after your saved details were sent"
            if result.get("escalate") or retry
            else "which I can't collect here"
            if uncollectable
            else ""
        )
        told = _escalate(
            client, row_id, employee, picked, f"LetsFG need_info, still asking for: {asked} {why}".strip()
        )
        _notify(
            client,
            employee_dm,
            f":warning: Not booked: LetsFG still wouldn't accept *{picked.get('summary', '')}* "
            f"(it asks for {asked}). Nothing was booked or charged. {told}",
        )
        return

    db.update(row_id, status="need_documents", details=details)
    value = json.dumps({"row": row_id})
    intro = _DOCS_INTRO_DOMESTIC if picked.get("domestic") else _DOCS_INTRO_INTL
    if restricted:
        intro = (
            f"{_PASSPORT_EVERYWHERE} If you have a passport, enter the details once — "
            "they're saved securely for future bookings. If you don't, the travel admin "
            "can book this flight for you manually."
        )
    text = (
        f":passport_control: To book *{picked.get('summary', '')}*, LetsFG needs: "
        f"{_labels(missing)}.\n{intro}\nNothing has been booked or charged yet."
    )
    needs_passport = any(f in traveller_profile.PASSPORT_FIELDS for f in missing)
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
        {
            "type": "actions",
            "block_id": f"traveldesk_docs_{row_id}",
            "elements": [
                {
                    "type": "button",
                    "style": "primary",
                    "action_id": "provide_documents",
                    "text": {
                        "type": "plain_text",
                        "text": "Enter passport details" if needs_passport else "Enter details",
                    },
                    "value": value,
                },
                _no_passport_button(row_id, restricted),
            ],
        },
    ]
    if employee_dm:
        try:
            client.chat_postMessage(channel=employee_dm, text=text, blocks=blocks)
            return
        except Exception as e:  # noqa: BLE001
            log.warning("could not post documents request for row %s: %s", row_id, type(e).__name__)
    _dm(client, employee, text, blocks)


def _row_for_actor(body, action, client, *, expect):
    """(row_id, row) if the clicking user owns the row and it's in `expect`, else None."""
    actor = body["user"]["id"]
    try:
        row_id = int(json.loads(action["value"])["row"])
    except (KeyError, ValueError, TypeError):
        return None
    row = db.get(row_id)
    if not row or row["slack_user"] != actor:
        log.warning("unauthorised %s row=%s user=%s", action.get("action_id"), row_id, actor)
        return None
    if row["status"] not in expect:
        _dm(client, actor, f"That request is already `{row['status']}`.")
        return None
    return row_id, row


@app.action("provide_documents")
def on_provide_documents(ack, body, client, action):
    ack()
    _open_documents_modal(body, client, action)


def _open_documents_modal(body, client, action):
    found = _row_for_actor(body, action, client, expect=("need_documents",))
    if not found:
        return
    row_id, row = found
    if not secure_store.is_configured():
        _dm(
            client,
            row["slack_user"],
            ":warning: Can't collect passport details: secure storage "
            "isn't configured (TRAVELDESK_DATA_KEY). Ask the admin.",
        )
        return
    picked = _row_details(row)
    missing = list(picked.get("missing_fields") or [])
    restricted = bool(picked.get("domestic")) and _passport_everywhere(row["slack_user"], picked)
    view = _passenger_modal(
        row_id,
        missing,
        {},
        traveller_profile.load(row["slack_user"]) or {},
        domestic=False,
        callback_id="passenger_documents",
        intro=_DOCS_INTRO_DOMESTIC if picked.get("domestic") else _DOCS_INTRO_INTL,
        required=set(missing),
        extra_blocks=[
            {
                "type": "actions",
                "block_id": "no_passport_block",
                "elements": [_no_passport_button(row_id, restricted)],
            }
        ],
    )
    try:
        client.views_open(trigger_id=body.get("trigger_id"), view=view)
    except Exception as e:  # noqa: BLE001
        log.warning("could not open documents modal for row %s: %s", row_id, type(e).__name__)
        _dm(client, row["slack_user"], ":warning: Couldn't open the form. Press the button again.")


@app.view("passenger_documents")
def on_passenger_documents(ack, body, client, view):
    _handle_documents_submission(ack, body, client, view)


def _handle_documents_submission(ack, body, client, view):
    actor = body["user"]["id"]
    try:
        meta = json.loads(view.get("private_metadata") or "{}")
        row_id = int(meta["row"])
    except (KeyError, ValueError, TypeError):
        ack()
        return
    row = db.get(row_id)
    if not row or row["slack_user"] != actor:
        ack()
        log.warning("unauthorised documents submission row=%s user=%s", row_id, actor)
        return
    if row["status"] != "need_documents":
        ack()
        _dm(client, actor, f"That request is already `{row['status']}`.")
        return
    picked = _row_details(row)
    shown = list(meta.get("fields") or [])
    submitted = {k: v for k, v in _submitted_values(view).items() if k in shown}
    merged = {**(traveller_profile.load(actor) or {}), **{k: v for k, v in submitted.items() if v}}
    res = traveller_profile.validate(
        merged,
        for_booking=True,
        travel_date=picked.get("depart_date"),
        last_flight_date=picked.get("last_flight_date"),
        passport_months=0 if picked.get("domestic") else traveller_profile.PASSPORT_VALIDITY_MONTHS,
        also=shown,
    )
    errors = {}
    for f in shown:
        if not submitted.get(f):
            errors[f] = "Required — LetsFG needs this for this fare"
        elif f in res.invalid:
            errors[f] = res.invalid[f]
    if errors:
        ack(response_action="errors", errors=errors)
        return
    try:
        secure_store.save(actor, {f: res.cleaned[f] for f in shown if f in res.cleaned})
    except secure_store.SecureStoreUnavailable:
        ack(
            response_action="errors",
            errors={shown[0]: "Secure storage is not configured — ask the admin."} if shown else {},
        )
        return
    ack()
    msg = (
        ":lock: Passport details saved securely for future bookings."
        if any(f in traveller_profile.PASSPORT_FIELDS for f in shown)
        else ":lock: Details saved securely for future bookings."
    )
    if "passport_number" in res.cleaned and "passport_number" in shown:
        msg += f" Passport on file: {traveller_profile.mask_passport(res.cleaned['passport_number'])}."
    _dm(client, actor, msg + " Retrying the booking…")
    threading.Thread(target=run_document_retry, args=(client, row_id, actor), daemon=True).start()


def run_document_retry(client, row_id, employee):
    """Retry the same offer with the newly saved details (once)."""
    employee_dm = None
    try:
        employee_dm = client.conversations_open(users=employee)["channel"]["id"]
    except Exception as e:  # noqa: BLE001
        log.warning("open DM to employee %s failed: %s", employee, e)
    if not db.transition(row_id, "need_documents", "booking_started"):
        return  # someone else is already retrying this row
    picked = _row_details(db.get(row_id))
    if config.DRY_RUN:
        db.update(row_id, status="need_documents")
        _notify(
            client,
            employee_dm,
            ":test_tube: *Dry run* — would retry the booking with your saved details. (DRY_RUN=true in .env)",
        )
        return
    try:
        result = letsfg_booking.book(picked, traveller_profile.load(employee) or {}, row_id)
    except Exception as e:  # noqa: BLE001
        log.exception("document retry failed for row %s", row_id)
        result = {"status": "failed", "message": f"{type(e).__name__}"}
    _handle_book_result(client, row_id, employee, employee_dm, picked, {**picked, **result}, retry=True)


# --- price check after an expired offer / changed price ----------------------


def _search_same_route(picked):
    origin, dest = picked.get("origin"), picked.get("destination")
    if not (origin and dest and picked.get("depart_date")):
        return {"status": "failed", "message": "the original route isn't recorded"}
    return letsfg_rest.search_flights(
        origin, dest, picked["depart_date"], return_from=picked.get("return_from")
    )


def _picked_from_offer(base, offer, search, **extra):
    details = flight_scope.offer_details(offer)
    return {
        **base,
        **details,
        "status": "picked",
        "backend": "rest",
        "search_id": search["search_id"],
        "offer_id": offer["id"],
        "price": flight_scope.offer_price(offer),
        "currency": offer.get("currency") or search.get("currency") or config.CURRENCY,
        "offer_link": offer.get("booking_url"),
        "last_flight_date": details["last_flight_date"] or base.get("last_flight_date"),
        **extra,
    }


def _new_confirmation(client, old_row, employee, employee_dm, picked):
    """New request row + confirmation card: the traveller approves again."""
    row_id, _ = db.new_request(employee, old_row["request"], old_row.get("provider") or config.AI_PROVIDER)
    db.update(
        row_id,
        status="awaiting_confirm",
        price=picked.get("price"),
        currency=picked.get("currency"),
        details=picked,
    )
    if not employee_dm:
        try:
            employee_dm = client.conversations_open(users=employee)["channel"]["id"]
        except Exception as e:  # noqa: BLE001
            log.warning("open DM to employee %s failed: %s", employee, e)
            return row_id
    traveller = traveller_profile.public_view(traveller_profile.load(employee) or {})
    _post_confirmation_card(client, row_id, employee, old_row["request"], picked, employee_dm, traveller)
    return row_id


def reprice_and_reconfirm(client, row_id, employee, employee_dm, picked, result):
    search = _search_same_route(picked)
    if _stopped(row_id):
        return
    if search.get("status") != "ok":
        _notify(
            client,
            employee_dm,
            ":warning: The fare expired and I couldn't re-check the price "
            f"({search.get('message', '')}). Nothing was booked or charged. Send the request again.",
        )
        return
    sig = picked.get("signature")
    match = next(
        (
            o
            for o in search["offers"]
            if isinstance(o, dict) and o.get("id") and [list(s) for s in flight_scope.signature(o)] == sig
        ),
        None,
    )
    if not match:
        _notify(
            client,
            employee_dm,
            f":warning: *{picked.get('summary', '')}* is no longer "
            "available. Nothing was booked or charged. Send the request again to search.",
        )
        return
    new_price = flight_scope.offer_price(match)
    if config.MAX_PRICE and (new_price is None or new_price > config.MAX_PRICE):
        _notify(
            client,
            employee_dm,
            f":warning: *{picked.get('summary', '')}* now costs "
            f"{new_price} — above MAX_PRICE. Nothing was booked or charged.",
        )
        return
    old_price = picked.get("price")
    cur = match.get("currency") or picked.get("currency") or ""
    if old_price is not None and new_price is not None and abs(float(new_price) - float(old_price)) >= 0.5:
        change = f"The price changed from {old_price} to {new_price} {cur}."
    else:
        change = f"The fare had expired; I re-checked it (still {new_price} {cur})."
    notice = (
        f":arrows_counterclockwise: *Please approve again.* {change} "
        "Your saved passport details will be sent with the booking."
    )
    keep = {
        k: picked[k]
        for k in (
            "request",
            "model",
            "provider",
            "summary",
            "depart_date",
            "origin",
            "destination",
            "return_from",
            "required_fields",
        )
        if k in picked
    }
    new_picked = _picked_from_offer(keep, match, search, notice=notice, book_attempt=0)
    _new_confirmation(client, db.get(row_id), employee, employee_dm, new_picked)


# --- "I don't have a passport": alternatives -----------------------------------


@app.action("no_passport")
def on_no_passport(ack, body, client, action):
    ack()
    _handle_no_passport(body, client, action)


def _handle_no_passport(body, client, action):
    found = _row_for_actor(body, action, client, expect=("need_documents",))
    if not found:
        return
    row_id, _row = found
    picked = _row_details(_row)
    if picked.get("domestic") and _passport_everywhere(_row["slack_user"], picked):
        # Alternatives would hit the same rule (an older message's button).
        _request_manual_booking(body, client, row_id, _row)
        return
    if not db.transition(row_id, "need_documents", "no_passport"):
        return
    view = body.get("view") or {}
    if view.get("id"):
        try:
            client.views_update(
                view_id=view["id"],
                view={
                    "type": "modal",
                    "title": {"type": "plain_text", "text": "Passport details"},
                    "close": {"type": "plain_text", "text": "Close"},
                    "blocks": [
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": "No problem — I'm looking for other flights on the same route and time. "
                                "I'll message you.",
                            },
                        }
                    ],
                },
            )
        except Exception as e:  # noqa: BLE001
            log.warning("views_update failed: %s", type(e).__name__)
    else:
        _update_card_as_done(client, body, "No passport — looking for other flights")
    threading.Thread(target=find_alternatives, args=(client, row_id, body["user"]["id"]), daemon=True).start()


@app.action("request_manual_booking")
def on_request_manual_booking(ack, body, client, action):
    ack()
    found = _row_for_actor(body, action, client, expect=("need_documents",))
    if found:
        _request_manual_booking(body, client, *found)


def _request_manual_booking(body, client, row_id, row):
    """No passport and LetsFG needs one everywhere: hand over to the travel admin."""
    if not db.transition(row_id, "need_documents", "needs_attention"):
        return
    employee = row["slack_user"]
    picked = _row_details(row)
    told = _escalate(
        client,
        row_id,
        employee,
        picked,
        "Traveller has no passport and LetsFG requires passport details for every "
        "booking, including domestic flights. Please book this flight manually.",
    )
    done = (
        f":information_source: {_PASSPORT_EVERYWHERE} I've asked for this flight to be "
        f"booked manually. Nothing was booked or charged. {told}"
    )
    view = body.get("view") or {}
    if view.get("id"):
        try:
            client.views_update(
                view_id=view["id"],
                view={
                    "type": "modal",
                    "title": {"type": "plain_text", "text": "Passport details"},
                    "close": {"type": "plain_text", "text": "Close"},
                    "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": done}}],
                },
            )
        except Exception as e:  # noqa: BLE001
            log.warning("views_update failed: %s", type(e).__name__)
    else:
        _update_card_as_done(client, body, "No passport — sent to the travel admin")
    _dm(client, employee, done)


def _in_window(offer, center: str | None) -> bool:
    if not center:
        return True
    dep = flight_scope.depart_at(offer)
    try:
        delta = abs((datetime.fromisoformat(dep) - datetime.fromisoformat(center)).total_seconds())
    except (TypeError, ValueError):
        return False
    return delta <= config.ALT_WINDOW_HOURS * 3600


def find_alternatives(client, row_id, employee):
    if _stopped(row_id):
        return
    row = db.get(row_id)
    picked = _row_details(row)
    try:
        employee_dm = client.conversations_open(users=employee)["channel"]["id"]
    except Exception as e:  # noqa: BLE001
        log.warning("open DM to employee %s failed: %s", employee, e)
        return
    if picked.get("domestic") and _passport_everywhere(employee, picked):
        db.update(row_id, status="needs_attention")
        told = _escalate(
            client,
            row_id,
            employee,
            picked,
            "Traveller has no passport and LetsFG requires passport details for "
            "every booking. Please book this flight manually.",
        )
        _notify(
            client,
            employee_dm,
            f":information_source: {_PASSPORT_EVERYWHERE} Nothing was booked or charged. {told}",
        )
        return
    search = _search_same_route(picked)
    if _stopped(row_id):
        return
    if search.get("status") != "ok":
        _notify(
            client,
            employee_dm,
            f":warning: Couldn't search for other flights "
            f"({search.get('message', '')}). Send the request again later.",
        )
        return
    sig = picked.get("signature")
    candidates = [
        o
        for o in search["offers"]
        if isinstance(o, dict)
        and o.get("id")
        and [list(s) for s in flight_scope.signature(o)] != sig
        and _in_window(o, picked.get("depart_at"))
        and (p := flight_scope.offer_price(o)) is not None
        and (not config.MAX_PRICE or p <= config.MAX_PRICE)
    ]
    history = {}
    for o in candidates:
        d = flight_scope.offer_details(o)
        history[o["id"]] = db.passport_history(d["airline"], d["fare_type"])
    unknown = [o for o in candidates if history[o["id"]] != "fare"]
    # Airlines never seen asking for passports first, then cheapest.
    unknown.sort(key=lambda o: (history[o["id"]] is not None, flight_scope.offer_price(o)))
    window = f"±{config.ALT_WINDOW_HOURS:g}h of the original flight"
    route = (
        f"{picked.get('origin', '?')} → {picked.get('destination', '?')} on {picked.get('depart_date', '?')}"
    )

    if not unknown:
        reason = (
            f"every other flight {route} ({window}) is on a fare that has asked for "
            f"passport details ({len(candidates)} checked)"
            if candidates
            else f"no other flights {route} ({window})"
        )
        db.update(row_id, status="needs_attention")
        told = _escalate(client, row_id, employee, picked, f"Traveller has no passport and {reason}.")
        _notify(
            client,
            employee_dm,
            f":warning: I couldn't find a flight you can book without a "
            f"passport: {reason}. Nothing was booked or charged. {told}",
        )
        return

    alts = []
    blocks = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Other flights {route}* ({window}).\n{_ALT_LABEL}"},
        }
    ]
    for i, o in enumerate(unknown[:5]):
        d = flight_scope.offer_details(o)
        alt = _picked_from_offer(
            {
                k: picked[k]
                for k in ("origin", "destination", "return_from", "depart_date", "provider")
                if k in picked
            },
            o,
            search,
            summary=d["auto_summary"],
            model="alternative",
            notice=_ALT_LABEL,
        )
        alts.append(alt)
        seen = (
            " · _this airline has asked for passports on other fares_"
            if history[o["id"]] == "airline"
            else ""
        )
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*{alt['summary']}*\n{alt['price']} {alt['currency']}{seen}",
                },
                "accessory": {
                    "type": "button",
                    "action_id": "choose_alternative",
                    "text": {"type": "plain_text", "text": "Choose this flight"},
                    "value": json.dumps({"row": row_id, "alt": i}),
                },
            }
        )
    skipped = len(candidates) - len(unknown)
    if skipped:
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"{skipped} other flight(s) hidden: their fares have asked for passport details before.",
                    }
                ],
            }
        )
    db.update(row_id, details={**picked, "alternatives": alts})
    try:
        client.chat_postMessage(channel=employee_dm, text=f"Other flights {route}", blocks=blocks)
    except Exception as e:  # noqa: BLE001
        log.warning("could not post alternatives for row %s: %s", row_id, type(e).__name__)


@app.action("choose_alternative")
def on_choose_alternative(ack, body, client, action):
    ack()
    _handle_choose_alternative(body, client, action)


def _handle_choose_alternative(body, client, action):
    found = _row_for_actor(body, action, client, expect=("no_passport",))
    if not found:
        return
    row_id, row = found
    try:
        idx = int(json.loads(action["value"])["alt"])
        alt = _row_details(row)["alternatives"][idx]
    except (KeyError, ValueError, TypeError, IndexError):
        return
    if not db.transition(row_id, "no_passport", "replaced"):
        return
    _update_card_as_done(client, body, f"Chose {alt.get('summary', 'an alternative')}")
    channel = body.get("channel", {}).get("id") or body.get("container", {}).get("channel_id")
    _new_confirmation(client, row, row["slack_user"], channel, alt)


def _resume_unfinished_bookings(client):
    """On startup, pick up any booking whose poll thread died with the last
    process. Re-attach a watch_booking thread so the PNR / failure message
    still reaches the employee."""
    resumed = 0
    for row in db.unfinished_bookings():
        booking_ref = row.get("booking_ref")
        if not booking_ref:
            continue
        employee = row.get("slack_user")
        try:
            employee_dm = client.conversations_open(users=employee)["channel"]["id"]
        except Exception as e:  # noqa: BLE001 — one row's DM failure must not stop startup resume
            log.warning("resume: could not open DM to %s: %s", employee, e)
            employee_dm = None
        try:
            domestic = bool(json.loads(row.get("details") or "{}").get("domestic"))
        except (ValueError, TypeError, AttributeError):
            domestic = False
        if row.get("status") == "awaiting_payment":
            threading.Thread(target=watch_payment, args=(client, row["id"]), daemon=True).start()
        else:
            threading.Thread(
                target=watch_booking,
                args=(client, row["id"], employee_dm, booking_ref),
                kwargs={"domestic": domestic},
                daemon=True,
            ).start()
        resumed += 1
    if resumed:
        log.info("resumed %d in-flight booking(s) after restart", resumed)


# --- cancelled from the web dashboard -------------------------------------------

_WEB_SWEEP_SECONDS = 5


def notify_dashboard_cancellations(client) -> int:
    """Tell travellers (and pay-link approvers) about requests cancelled from the
    web UI (dashboard Cancel, or /book "Change flight"). The background steps stop by themselves (see `_stopped`)."""
    rows = db.dashboard_cancellations()
    for row in rows:
        try:
            _tell_dashboard_cancel(client, row)
        except Exception:  # noqa: BLE001 — one row's Slack failure must not block the others
            log.exception("could not report dashboard cancel for row %s", row["id"])
        db.mark_cancel_notified(row["id"])
    return len(rows)


def _tell_dashboard_cancel(client, row):
    """One short message to the traveller (in the request's thread), the card's
    buttons removed, and pay-link approvers told not to pay."""
    row_id, employee = row["id"], row.get("slack_user")
    details = _row_details(row)
    what = details.get("summary") or row.get("request") or f"request #{row_id}"
    pay_link = bool(row.get("pay_url")) and row.get("payment_status") == "awaiting_payment"
    changed = row.get("approver") == db.WEB_CHANGE
    log.info("row %s cancelled from the web (%s); telling %s", row_id, row.get("approver"), employee)

    if changed:
        text = (
            f":arrows_counterclockwise: Flight changed on the web page, so #{row_id} *{what}* was cancelled."
        )
        card_note = ":arrows_counterclockwise: Replaced: flight changed on the web page"
    else:
        text = f":no_entry: #{row_id} *{what}* was cancelled from the travel desk dashboard."
        card_note = ":no_entry: Cancelled from the travel desk dashboard"
    if pay_link:
        text += " Don't use its payment link."
    elif not row.get("booking_ref"):
        text += " Nothing was booked or charged."

    # The confirmation card loses its buttons, so nobody can confirm a cancelled trip.
    channel, card = row.get("manager_channel"), row.get("manager_message_ts")
    if channel and card:
        done = f"{details.get('summary', '')}\n{card_note}"
        try:
            client.chat_update(
                channel=channel,
                ts=card,
                text=done,
                blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": done}}],
            )
        except Exception as e:  # noqa: BLE001
            log.warning("could not update card for row %s: %s", row_id, type(e).__name__)

    _thread_note(client, row, text)

    if pay_link:
        db.update(row_id, payment_status="unpaid")
        for user in _approvers(row):
            if user != employee:
                _dm(
                    client,
                    user,
                    f":no_entry: #{row_id} (<@{employee}>) was cancelled. Don't pay its payment link.",
                )


def _thread_note(client, row, text):
    """A short line to the traveller in the request's thread (plain DM as fallback)."""
    employee, thread = row.get("slack_user"), row.get("slack_thread_ts")
    dm = _open_dm(client, employee)
    if dm:
        try:
            client.chat_postMessage(channel=dm, text=text, **({"thread_ts": thread} if thread else {}))
            return
        except Exception as e:  # noqa: BLE001 — fall back to a plain DM
            log.warning("thread reply failed for row %s: %s", row["id"], type(e).__name__)
    _dm(client, employee, text)


# --- confirmed on the /book page ----------------------------------------------------------


def start_web_confirmed(client) -> int:
    """Book requests the traveller confirmed on the web page, exactly as a Slack
    Confirm click does (the web checked their passenger details first)."""
    started = 0
    for row in db.web_confirmed():
        if not db.claim_status(row["id"], (db.WEB_CONFIRMED,), "confirmed"):
            continue  # cancelled meanwhile, or another sweep got it
        try:
            _book_web_confirmed(client, db.get(row["id"]))
            started += 1
        except Exception:  # noqa: BLE001 — one row must not block the others
            log.exception("could not start web-confirmed booking for row %s", row["id"])
    return started


def _book_web_confirmed(client, row):
    row_id, employee = row["id"], row["slack_user"]
    picked = _row_details(row)
    traveller = traveller_profile.load(employee) or {}
    log.info("row %s confirmed on the web page by %s; booking", row_id, employee)
    channel, card = row.get("manager_channel"), row.get("manager_message_ts")
    if channel and card:
        # It was sent to Slack first: clear the card's buttons and say where it was confirmed.
        # (Confirmed straight from the page, its own Slack note already says so.)
        note = f":lock: Confirmed on the web page by <@{employee}>"
        blocks = [
            b
            for b in _confirm_blocks(
                row_id, employee, row["request"], picked, traveller_profile.public_view(traveller)
            )
            if b.get("type") != "actions"
        ]
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": note}]})
        try:
            client.chat_update(channel=channel, ts=card, text=f"Booking {note}", blocks=blocks)
        except Exception as e:  # noqa: BLE001
            log.warning("could not update card for row %s: %s", row_id, type(e).__name__)
        _thread_note(client, row, f":white_check_mark: #{row_id} confirmed on the web page. Booking it now.")
    threading.Thread(
        target=run_booking, args=(client, row_id, employee, picked, traveller), daemon=True
    ).start()


def _web_actions_watcher(client, stop: threading.Event, interval: float) -> None:
    """Acts on what the web UI wrote: confirmations (book) and cancellations (tell)."""
    while not stop.wait(interval):
        for step in (start_web_confirmed, notify_dashboard_cancellations):
            try:
                step(client)
            except Exception:  # noqa: BLE001 — keep watching
                log.exception("web action check failed: %s", step.__name__)


def _heartbeat(stop: threading.Event, interval: float, started: float) -> None:
    """Log one line every `interval` seconds so a stalled process shows up in bot.log."""
    while not stop.wait(interval):
        log.info(
            "heartbeat: alive, uptime %ds, threads %d", time.monotonic() - started, threading.active_count()
        )


def main() -> None:
    """Start the bot (Socket Mode). Run with: python -m traveldesk"""
    log.info(
        "TravelDesk bot starting (provider=%s, DRY_RUN=%s)",
        config.AI_PROVIDER,
        config.DRY_RUN,
    )
    traveller_profile.check_travellers_file()
    if not secure_store.is_configured():
        log.warning("TRAVELDESK_DATA_KEY not set: passport details cannot be collected or stored")
    _resume_unfinished_bookings(app.client)
    start_web_confirmed(app.client)
    notify_dashboard_cancellations(app.client)
    threading.Thread(
        target=_web_actions_watcher,
        args=(app.client, threading.Event(), _WEB_SWEEP_SECONDS),
        daemon=True,
        name="web-actions",
    ).start()
    if config.HEARTBEAT_SECONDS > 0:
        threading.Thread(
            target=_heartbeat,
            args=(threading.Event(), config.HEARTBEAT_SECONDS, time.monotonic()),
            daemon=True,
            name="heartbeat",
        ).start()
    SocketModeHandler(app, config.SLACK_APP_TOKEN).start()
