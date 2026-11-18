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
from datetime import date, datetime

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

import agent as agent_factory
import config
import db
import flight_scope
import letsfg_booking
import letsfg_rest
import secure_store
import traveller_profile

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

    Everything else (channel messages, edits, deletions, the bot's own
    posts, thread replies) is ignored quietly."""
    if event.get("channel_type") != "im":
        return
    if event.get("subtype") or event.get("bot_id") or not event.get("user"):
        return  # edits, deletions, joins, and messages posted by bots (incl. this one)
    if event.get("thread_ts") and event.get("thread_ts") != event.get("ts"):
        client.chat_postMessage(
            channel=event["channel"],
            thread_ts=event["thread_ts"],
            text="To start a new request, send it as a new message (not a thread reply).",
        )
        return
    start_request(client, event["user"], (event.get("text") or "").strip(), event["channel"])


def start_request(client, user, text, dm):
    """Shared by /traveldesk and DMs: checks, then search on a background thread."""
    if text.lower().strip(" !.") in _HELP_WORDS:
        client.chat_postMessage(channel=dm, text=HELP)
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
    row_id, _key = db.new_request(user, text, config.AI_PROVIDER)
    try:
        agent = agent_factory.get_agent()
        picked = _call_agent_pick(agent, text, traveller, row_id)
    except Exception as e:  # noqa: BLE001 — surfaced to user
        log.exception("pick failed for row %s", row_id)
        db.update(row_id, status="failed", details={"error": str(e)})
        say(client, channel, thread_ts, f":x: Search failed: {e}")
        return

    status = picked.get("status")
    if status != "picked":
        db.update(row_id, status=status or "failed", details=picked)
        say(
            client,
            channel,
            thread_ts,
            f":warning: Not booked ({status}). {picked.get('message', '')}\n{picked.get('summary', '')}",
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


def _confirm_blocks(row_id, employee, request_text, picked, traveller):
    price = picked.get("price")
    currency = picked.get("currency") or config.CURRENCY
    price_line = f"{price} {currency}" if price is not None else "price n/a"
    traveller_line = f"{traveller.get('given_name', '')} {traveller.get('family_name', '')}".strip()
    offer_link = picked.get("offer_link")
    value = json.dumps({"row": row_id})
    fields = [
        {"type": "mrkdwn", "text": f"*Traveller*\n{traveller_line}"},
        {"type": "mrkdwn", "text": f"*Price*\n{price_line}"},
        {"type": "mrkdwn", "text": f"*Request*\n{request_text}"},
        {"type": "mrkdwn", "text": f"*Model*\n`{picked.get('model') or picked.get('provider', '?')}`"},
    ]
    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "Confirm your booking"},
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*{picked.get('summary', '(no summary)')}*"},
        },
        {"type": "section", "fields": fields},
    ]
    if offer_link:
        blocks.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f"<{offer_link}|View offer on LetsFG>"}],
            }
        )
    if picked.get("notice"):
        blocks.insert(1, {"type": "section", "text": {"type": "mrkdwn", "text": picked["notice"]}})
    warning = _passport_warning(picked, employee)
    if warning:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": warning}]})
    confirm_text = (
        f"This will initiate a real booking for {price_line}."
        if not config.DRY_RUN
        else f"DRY_RUN is on. This will search and pick only, no real booking for {price_line}."
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


_GENDER_OPTIONS = [_option("m", "Male"), _option("f", "Female")]
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
        if f == "passport_issue":
            optional = True  # optional unless LetsFG asked for it
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
    status = result.get("status", "failed")
    if status == "need_info" and ("missing_fields" in result or result.get("escalate")):
        _handle_need_info(client, row_id, employee, employee_dm, picked, result, retry=retry)
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
            db.update(
                row_id,
                status="completed",
                pnr=st.get("pnr"),
                payment_status="paid",
                charged_amount=charged,
                details=st,
            )
            text = (
                f":ticket: *Ticket issued!* PNR: *{st.get('pnr')}*\nTotal: {charged} {st.get('currency', '')}"
            )
            if st.get("ticket_url"):
                text += f"\nTicket: {st['ticket_url']}"
            if domestic:
                text += f"\n:identification_card: {_DOMESTIC_ID_LINE}"
            _notify(client, employee_dm, text)
            return
        if state in ("failed", "approval_expired", "not_started", "needs_attention", "cancelled"):
            db.update(
                row_id, status=state, payment_status="failed" if state == "failed" else "unpaid", details=st
            )
            _notify(client, employee_dm, f":warning: Booking {state}. {st.get('message', '')}")
            return
    db.update(row_id, status="timeout")
    _notify(
        client,
        employee_dm,
        ":hourglass: Still not ticketed after the waiting time. Check your LetsFG email/account.",
    )


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
        threading.Thread(
            target=watch_booking,
            args=(client, row["id"], employee_dm, booking_ref),
            kwargs={"domestic": domestic},
            daemon=True,
        ).start()
        resumed += 1
    if resumed:
        log.info("resumed %d in-flight booking(s) after restart", resumed)


if __name__ == "__main__":
    log.info(
        "TravelDesk bot starting (provider=%s, DRY_RUN=%s)",
        config.AI_PROVIDER,
        config.DRY_RUN,
    )
    traveller_profile.check_travellers_file()
    if not secure_store.is_configured():
        log.warning("TRAVELDESK_DATA_KEY not set: passport details cannot be collected or stored")
    _resume_unfinished_bookings(app.client)
    SocketModeHandler(app, config.SLACK_APP_TOKEN).start()
