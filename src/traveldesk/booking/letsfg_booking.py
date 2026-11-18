"""Deterministic LetsFG booking via the Developer REST API.

The payment-affecting steps — book and booking status — are plain Python,
never an LLM:
* the passenger payload comes from the validated traveller profile, never
  from a model;
* LetsFG's need_info answers are handled here (retry once with saved
  details, otherwise ask the traveller for exactly the missing fields);
* success is never reported without a booking_id / PNR from LetsFG.
"""

from __future__ import annotations

import logging
import uuid

from traveldesk.booking import traveller_profile
from traveldesk.clients import letsfg_rest
from traveldesk.storage import db

log = logging.getLogger("letsfg_booking")


def idempotency_key(booking_id: int | None, attempt: int = 0) -> str:
    """Stable per booking row, so any retry of the same row reuses the key and
    LetsFG returns the existing booking instead of creating a second one.

    `attempt` moves on only after LetsFG answered need_info, i.e. created no
    booking, so a fresh key for the corrected request cannot double-book."""
    if booking_id is None:
        return f"td-{uuid.uuid4().hex}"
    row = db.get(booking_id) or {}
    suffix = f"-r{attempt}" if attempt else ""
    return f"td-{booking_id}-{row.get('request_key') or 'x'}"[: 60 - len(suffix)] + suffix


def book(picked: dict, traveller: dict, booking_id: int | None = None) -> dict:
    """Book a REST-picked offer. Offers from the retired MCP flow (no
    "backend": "rest") can't be booked here: their IDs aren't REST IDs."""
    if picked.get("backend") == "rest":
        row = db.get(booking_id) if booking_id is not None else None
        if row and (row.get("pay_url") or row.get("status") == "awaiting_payment"):
            # A pay link is open for this request: paying it starts the booking.
            # Booking again would charge the saved card again.
            log.warning("booking row=%s refused: a LetsFG pay link is open for it", booking_id)
            return {
                "status": "awaiting_payment",
                "booking_ref": row.get("booking_ref"),
                "message": "This booking is waiting for payment on its secure link; not booking again.",
            }
        return _book_rest(picked, traveller, booking_id)
    log.warning("booking row=%s refused: offer was not picked via the LetsFG REST API", booking_id)
    return {
        "status": "failed",
        "message": "This request was made with an older version of the bot and can't be "
        "booked. Please send the request again.",
    }


def _book_rest(picked: dict, traveller: dict, booking_id: int | None) -> dict:
    """REST booking with the need_info loop.

    1. Book with the fields the trip needs (domestic: no passport), plus any
       fields an earlier need_info for this booking asked for.
    2. On need_info: log it; if the saved profile already has every field
       asked for, retry once with them. Otherwise return need_info with
       `missing_fields` = exactly the fields still needed from the traveller.
    3. A need_info on the retry returns `escalate=True`.
    Document fields are only ever real saved values — never placeholders."""
    domestic = bool(picked.get("domestic"))
    trip = dict(travel_date=picked.get("depart_date"), last_flight_date=picked.get("last_flight_date"))
    res = traveller_profile.validate(traveller, for_booking=True, domestic=domestic, **trip)
    if not res.ok:
        return {
            "status": "need_info",
            "domestic": domestic,
            "message": "Passenger details incomplete — " + res.summary(),
        }
    passenger = dict(res.cleaned)
    if domestic:
        # Flights within India: no passport / nationality data unless asked for.
        passenger = {k: v for k, v in passenger.items() if not _is_document_field(k)}
    months = 0 if domestic else traveller_profile.PASSPORT_VALIDITY_MONTHS
    asked = list(picked.get("required_fields") or [])
    known = passport_restriction(_slack_user(booking_id)) if domestic else None
    if known:
        # Already learnt: LetsFG wants these on every booking for this traveller.
        asked += [f for f in known.get("fields") or [] if f not in asked]
    if asked:
        have, need = traveller_profile.saved_fields(traveller, asked, passport_months=months, **trip)
        if need:
            return {
                "status": "need_info",
                "domestic": domestic,
                "missing_fields": need,
                "required_fields": asked,
                "passport_required_everywhere": bool(known),
                "book_attempt": int(picked.get("book_attempt") or 0),
                "message": "LetsFG needs more passenger details: " + ", ".join(need) + ".",
            }
        passenger.update(have)

    attempt = int(picked.get("book_attempt") or 0)
    out = _rest_call(picked, passenger, booking_id, attempt)
    if out.get("status") != "need_info" or "missing_fields" not in out:
        return {**out, "domestic": domestic, "book_attempt": attempt}

    missing = out["missing_fields"]
    have, need = traveller_profile.saved_fields(traveller, missing, passport_months=months, **trip)
    new = {k: v for k, v in have.items() if passenger.get(k) != v}
    if missing and not need and new:
        log.info(
            "booking row=%s: LetsFG asked for %s; retrying once with saved values",
            booking_id,
            ", ".join(missing),
        )
        passenger.update(have)
        attempt += 1
        retry = _rest_call(picked, passenger, booking_id, attempt)
        retry.update(
            domestic=domestic, book_attempt=attempt, required_fields=sorted(set(asked) | set(missing))
        )
        if retry.get("status") == "need_info":
            retry["escalate"] = True
        return retry
    return {
        "status": "need_info",
        "domestic": domestic,
        "book_attempt": attempt,
        "passport_required_everywhere": bool(out.get("passport_required_everywhere")),
        # Already sent but still refused → can't be fixed by asking again.
        "escalate": not need,
        "missing_fields": need or missing,
        "required_fields": sorted(set(asked) | set(missing)),
        "message": out.get("message", ""),
    }


def _rest_call(picked: dict, passenger: dict, booking_id, attempt: int) -> dict:
    out = letsfg_rest.book_flight(
        picked.get("search_id"), picked.get("offer_id"), passenger, idempotency_key(booking_id, attempt)
    )
    detail = out.pop("error_detail", None) or {}
    if out.get("status") == "need_info":
        fields = out.get("missing_fields") or detail.get("fields") or []
        log.warning(
            "LetsFG need_info row=%s airline=%s fare=%s domestic=%s HTTP %s error=%s fields=%s message=%s",
            booking_id,
            picked.get("airline"),
            picked.get("fare_type"),
            bool(picked.get("domestic")),
            detail.get("http_status"),
            detail.get("error"),
            fields,
            detail.get("server_message_full") or detail.get("server_message"),
        )
        try:
            db.log_need_info(
                booking_id=booking_id,
                airline=picked.get("airline"),
                airline_name=picked.get("airline_name"),
                fare_type=picked.get("fare_type"),
                route="-".join(
                    picked.get("airports") or [picked.get("origin") or "?", picked.get("destination") or "?"]
                ),
                domestic=picked.get("domestic"),
                price=picked.get("price"),
                currency=picked.get("currency"),
                fields=fields,
                http_status=detail.get("http_status"),
                error=detail.get("error"),
            )
        except Exception as e:  # noqa: BLE001 — logging must never block booking
            log.warning("could not record need_info: %s", type(e).__name__)
        out["passport_required_everywhere"] = _note_passport_rule(
            booking_id, fields, detail.get("server_message_full") or detail.get("server_message")
        )
    elif out.get("status") in ("payment_link", "payment_blocked"):
        log.warning(
            "LetsFG book row=%s: %s booking=%s declined=%s kind=%s HTTP %s error=%s message=%s",
            booking_id,
            out["status"],
            out.get("booking_ref"),
            out.get("declined_booking_ref"),
            out.get("payment_kind"),
            detail.get("http_status"),
            detail.get("error"),
            detail.get("server_message_full") or detail.get("server_message"),
        )
    elif detail:
        log.warning(
            "LetsFG book refused row=%s: HTTP %s error=%s fields=%s message=%s",
            booking_id,
            detail.get("http_status"),
            detail.get("error"),
            detail.get("fields"),
            detail.get("server_message_full") or detail.get("server_message"),
        )
    return out


PROVIDER = "letsfg"


def _slack_user(booking_id):
    try:
        row = db.get(booking_id) if booking_id is not None else None
    except Exception:  # noqa: BLE001
        row = None
    return row["slack_user"] if row else None


def _note_passport_rule(booking_id, fields, message) -> bool:
    """Detect LetsFG requiring a passport on every booking and remember it for
    this traveller: either LetsFG says so explicitly, or 2+ airlines asked."""
    if not any("passport" in str(f).lower() for f in fields):
        return False
    try:
        user = _slack_user(booking_id)
        if not user:
            return letsfg_rest.passport_required_for_everyone(message)
        if letsfg_rest.passport_required_for_everyone(message):
            source = "letsfg_message"
        elif len(db.passport_airlines_for_user(user)) >= 2:
            source = "multiple_airlines"
        else:
            return bool(db.get_restriction(user, PROVIDER, db.PASSPORT_REQUIRED))
        db.record_restriction(user, PROVIDER, db.PASSPORT_REQUIRED, fields=fields, source=source)
        log.warning("LetsFG requires passport details for every booking of user=%s (source=%s)", user, source)
        return True
    except Exception as e:  # noqa: BLE001 — detection must never block booking
        log.warning("could not record passport rule: %s", type(e).__name__)
        return False


def passport_restriction(slack_user) -> dict | None:
    """The saved rule, or — from need_info history — 2+ airlines having asked
    this traveller for a passport (then saved, so it's not recomputed)."""
    if not slack_user:
        return None
    try:
        known = db.get_restriction(slack_user, PROVIDER, db.PASSPORT_REQUIRED)
        if known:
            return known
        asked = db.passport_requests_for_user(slack_user)
        if len(asked) < 2:
            return None
        fields = list(dict.fromkeys(f for names in asked.values() for f in names))
        db.record_restriction(
            slack_user, PROVIDER, db.PASSPORT_REQUIRED, fields=fields, source="multiple_airlines"
        )
        log.warning(
            "LetsFG requires passport details for every booking of user=%s "
            "(source=multiple_airlines, from history)",
            slack_user,
        )
        return db.get_restriction(slack_user, PROVIDER, db.PASSPORT_REQUIRED)
    except Exception:  # noqa: BLE001
        return None


def _is_document_field(name: str) -> bool:
    n = str(name).lower()
    return any(w in n for w in ("passport", "nationality", "document", "identity"))


def status(booking_ref: str, booking_id: int | None = None) -> dict:
    return letsfg_rest.get_booking(booking_ref)


REQUIRED_PASSENGER_FIELDS = ("given_name", "family_name", "email")


def validate_traveller(traveller: dict | None) -> str | None:
    """Return a human-readable error string if traveller is missing required
    fields; else None. Never logs the values themselves."""
    if not traveller or not isinstance(traveller, dict):
        return "traveller profile is missing"
    missing = [f for f in REQUIRED_PASSENGER_FIELDS if not str(traveller.get(f, "")).strip()]
    if missing:
        return f"traveller profile is missing required fields: {', '.join(missing)}"
    return None
