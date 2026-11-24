"""Travellers, and the web's ways into the booking flow:

  * send_to_slack     — post the standard Slack card; confirm in Slack or on the page
  * confirm_and_book  — confirm on the page straight away (no Slack card)
  * confirm_on_web    — confirm on the page a request already sent to Slack

Confirming only marks the row `web_confirmed`; the Slack bot books it within
seconds exactly as after a Slack Confirm click (slack/app.py start_web_confirmed).
"""

from __future__ import annotations

import json
import logging

from traveldesk import config
from traveldesk.booking import traveller_profile
from traveldesk.storage import db
from traveldesk.web import offers as offer_logic
from traveldesk.web import passenger, slack_out
from traveldesk.web.errors import BookingError
from traveldesk.web.searches import Search

__all__ = [
    "BookingError",
    "confirm_and_book",
    "confirm_on_web",
    "require_traveller",
    "send_to_slack",
    "travellers",
]

log = logging.getLogger("traveldesk.web.booking")


def _name(profile: dict, user: str) -> str:
    return " ".join(p for p in (profile.get("given_name"), profile.get("family_name")) if p) or user


def travellers() -> list[dict]:
    """Everyone in travellers.json: Slack ID, name, and whether their profile can
    search (field names only, never values)."""
    out = []
    for user, base in config.TRAVELLERS.items():
        check = traveller_profile.validate(traveller_profile.load(user), for_booking=False)
        out.append(
            {
                "id": user,
                "name": _name(base, user),
                "ready": check.ok,
                "problems": check.summary(),
                # yes/no only: the passport number never leaves the server
                "passport_on_file": bool(str(base.get("passport_number") or "").strip()),
            }
        )
    return sorted(out, key=lambda t: t["name"].lower())


def require_traveller(user: str) -> dict:
    """The same checks as a Slack request before it may search."""
    profile = traveller_profile.load(user) if user in config.TRAVELLERS else None
    if not profile:
        raise BookingError(403, "This person isn't set up in travellers.json.")
    check = traveller_profile.validate(profile, for_booking=False)
    if not check.ok:
        raise BookingError(422, f"Their travellers.json entry needs fixing first — {check.summary()}.")
    return profile


def _chosen(search: Search, offer_id: str, user: str) -> tuple[dict, dict]:
    """(profile, pick record) for a new request, after the checks a Slack request gets."""
    if search.user != user:
        raise BookingError(403, "This search belongs to someone else.")
    if search.status != "ok" or not search.search_id:
        raise BookingError(409, "This search has no flights to book.")
    offer = search.offer(offer_id)
    if not offer:
        raise BookingError(404, "That flight isn't in this search any more.")
    profile = require_traveller(user)
    dup = db.recent_pending_for(user, search.text)
    if dup:
        raise BookingError(
            409, f"You already have a request in flight for this (#{dup['id']}, {dup['status']})."
        )
    return profile, offer_logic.pick_record(search.route, search.search_id, offer, offers_found=search.found)


def _ready_to_book(user: str, profile: dict, picked: dict, details: dict | None) -> bool:
    """Save typed passenger details (if any), then insist the profile can book this
    trip: 409 with `needs` (the form) while details are missing. True if saved."""
    saved = False
    if details:
        profile = passenger.save(user, profile, picked, details)
        saved = True
    needs = passenger.form(profile, picked)
    if needs:
        raise BookingError(409, "Passenger details are needed to book this flight.", needs=needs)
    return saved


def send_to_slack(search: Search, offer_id: str, user: str, slack_client=None) -> int:
    """Create the request row and post the Slack confirmation card for the chosen offer."""
    profile, picked = _chosen(search, offer_id, user)
    slack_client = slack_client or slack_out.client()
    dm = slack_out.open_dm(slack_client, user)
    thread_ts = slack_out.post_intro(slack_client, dm, search.text)
    row_id, _key = db.new_request(user, search.text, config.AI_PROVIDER, thread_ts=thread_ts)
    db.update(
        row_id, status="awaiting_confirm", price=picked["price"], currency=picked["currency"], details=picked
    )
    slack_out.post_card(
        slack_client, row_id, user, search.text, picked, dm, traveller_profile.public_view(profile)
    )
    log.info("web request #%s for %s sent to Slack for confirmation", row_id, user)
    return row_id


def confirm_and_book(
    search: Search, offer_id: str, user: str, details: dict | None = None, slack_client=None
) -> int:
    """Confirm the chosen flight on the page, without a Slack card. Slack gets a
    short note that starts the request's thread; later updates go there as usual."""
    profile, picked = _chosen(search, offer_id, user)
    saved = _ready_to_book(user, profile, picked, details)
    row_id, _key = db.new_request(user, search.text, config.AI_PROVIDER)
    db.update(
        row_id, status="awaiting_confirm", price=picked["price"], currency=picked["currency"], details=picked
    )
    db.confirm_from_web(row_id, user)  # the bot books rows in this state
    note = (
        f":airplane: #{row_id} *{search.text}*\n{picked['summary']} · {picked['price']:.0f} {picked['currency']}\n"
        ":white_check_mark: Confirmed on the travel desk web page. Booking it now."
        + ("\n:lock: Passenger details saved securely." if saved else "")
    )
    try:
        slack_client = slack_client or slack_out.client()
        ts = slack_out.post_note(slack_client, slack_out.open_dm(slack_client, user), note)
        db.update(row_id, slack_thread_ts=ts)
    except Exception as e:  # noqa: BLE001 — a missed note must not stop the booking
        log.warning("could not post web confirmation note for #%s: %s", row_id, type(e).__name__)
    log.info("web request #%s for %s confirmed on the web page", row_id, user)
    return row_id


def _note(row: dict, text: str, slack_client=None) -> None:
    """A short line in the request's Slack thread. Never blocks the web action."""
    try:
        slack_client = slack_client or slack_out.client()
        dm = slack_out.open_dm(slack_client, row["slack_user"])
        slack_out.post_note(slack_client, dm, text, thread_ts=row.get("slack_thread_ts"))
    except Exception as e:  # noqa: BLE001
        log.warning("could not post Slack note for #%s: %s", row["id"], type(e).__name__)


def confirm_on_web(row_id: int, user: str, details: dict | None = None, slack_client=None) -> dict:
    """Confirm on the page a request that was first sent to Slack. The bot posts
    the "confirmed" note when it starts booking; saved details are noted here."""
    row = db.get(row_id)
    if not row:
        raise BookingError(404, "Booking not found.")
    if row["slack_user"] != user:
        raise BookingError(403, "Only the traveller can confirm this booking.")
    if row["status"] != "awaiting_confirm":
        raise BookingError(409, f"It's {row['status'].replace('_', ' ')} now, so it can't be confirmed here.")
    saved = _ready_to_book(user, require_traveller(user), json.loads(row["details"] or "{}"), details)
    if not db.confirm_from_web(row_id, user):
        raise BookingError(409, "It changed meanwhile. Refresh and try again.")
    if saved:
        _note(row, ":lock: Passenger details saved securely on the travel desk website.", slack_client)
    log.info("web request #%s confirmed on the web page by %s", row_id, user)
    return {"booking_id": row_id, "status": db.WEB_CONFIRMED}
