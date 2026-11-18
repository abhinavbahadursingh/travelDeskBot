"""Deterministic LetsFG booking helpers.

Both provider agents (Claude and Groq) call these for the payment-affecting
steps — `book_flight` and `get_flight_booking` — instead of letting the LLM
drive the tool calls itself.

Why:
* The LLM can forget to pass passengers / contact_email, or try to invent a
  booking summary before the airline PNR exists.
* Deterministic Python here lets us validate traveller details up front,
  detect the "Developer API key" error cleanly, and refuse to report success
  without a confirmed PNR.

The underlying transport is the project's own `mcp_client.MCPClient` against
`LETSFG_MCP_URL` using the `LETSFG_TOKEN` bearer header. Anthropic's native
MCP connector is still used for the *search* phase in `claude_agent`.
"""
from __future__ import annotations

import logging
from typing import Any

import json
import uuid

import config
import db
import letsfg_rest
import mcp_client
import traveller_profile

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
    return f"td-{booking_id}-{row.get('request_key') or 'x'}"[:60 - len(suffix)] + suffix


def _backend_for(booking_id: int | None) -> str:
    if booking_id is not None:
        row = db.get(booking_id) or {}
        try:
            details = json.loads(row.get("details") or "{}")
        except (TypeError, ValueError):
            details = {}
        if isinstance(details, dict) and details.get("backend") in ("rest", "mcp"):
            return details["backend"]
    return config.LETSFG_BACKEND


def book(picked: dict, traveller: dict, booking_id: int | None = None) -> dict:
    """Book via the backend that produced the offer (IDs aren't portable)."""
    search_id, offer_id = picked.get("search_id"), picked.get("offer_id")
    if picked.get("backend") == "rest":
        return _book_rest(picked, traveller, booking_id)
    return book_flight(search_id, offer_id, traveller)


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
    trip = dict(travel_date=picked.get("depart_date"),
                last_flight_date=picked.get("last_flight_date"))
    res = traveller_profile.validate(traveller, for_booking=True, domestic=domestic, **trip)
    if not res.ok:
        return {"status": "need_info", "domestic": domestic,
                "message": "Passenger details incomplete — " + res.summary()}
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
            return {"status": "need_info", "domestic": domestic, "missing_fields": need,
                    "required_fields": asked,
                    "passport_required_everywhere": bool(known),
                    "book_attempt": int(picked.get("book_attempt") or 0),
                    "message": "LetsFG needs more passenger details: " + ", ".join(need) + "."}
        passenger.update(have)

    attempt = int(picked.get("book_attempt") or 0)
    out = _rest_call(picked, passenger, booking_id, attempt)
    if out.get("status") != "need_info" or "missing_fields" not in out:
        return {**out, "domestic": domestic, "book_attempt": attempt}

    missing = out["missing_fields"]
    have, need = traveller_profile.saved_fields(traveller, missing, passport_months=months, **trip)
    new = {k: v for k, v in have.items() if passenger.get(k) != v}
    if missing and not need and new:
        log.info("booking row=%s: LetsFG asked for %s; retrying once with saved values",
                 booking_id, ", ".join(missing))
        passenger.update(have)
        attempt += 1
        retry = _rest_call(picked, passenger, booking_id, attempt)
        retry.update(domestic=domestic, book_attempt=attempt,
                     required_fields=sorted(set(asked) | set(missing)))
        if retry.get("status") == "need_info":
            retry["escalate"] = True
        return retry
    return {"status": "need_info", "domestic": domestic, "book_attempt": attempt,
            "passport_required_everywhere": bool(out.get("passport_required_everywhere")),
            # Already sent but still refused → can't be fixed by asking again.
            "escalate": not need,
            "missing_fields": need or missing,
            "required_fields": sorted(set(asked) | set(missing)),
            "message": out.get("message", "")}


def _rest_call(picked: dict, passenger: dict, booking_id, attempt: int) -> dict:
    out = letsfg_rest.book_flight(picked.get("search_id"), picked.get("offer_id"), passenger,
                                  idempotency_key(booking_id, attempt))
    detail = out.pop("error_detail", None) or {}
    if out.get("status") == "need_info":
        fields = out.get("missing_fields") or detail.get("fields") or []
        log.warning("LetsFG need_info row=%s airline=%s fare=%s domestic=%s HTTP %s error=%s "
                    "fields=%s message=%s", booking_id, picked.get("airline"),
                    picked.get("fare_type"), bool(picked.get("domestic")),
                    detail.get("http_status"), detail.get("error"), fields,
                    detail.get("server_message_full") or detail.get("server_message"))
        try:
            db.log_need_info(
                booking_id=booking_id, airline=picked.get("airline"),
                airline_name=picked.get("airline_name"), fare_type=picked.get("fare_type"),
                route="-".join(picked.get("airports") or [picked.get("origin") or "?",
                                                          picked.get("destination") or "?"]),
                domestic=picked.get("domestic"), price=picked.get("price"),
                currency=picked.get("currency"), fields=fields,
                http_status=detail.get("http_status"), error=detail.get("error"))
        except Exception as e:  # noqa: BLE001 — logging must never block booking
            log.warning("could not record need_info: %s", type(e).__name__)
        out["passport_required_everywhere"] = _note_passport_rule(
            booking_id, fields, detail.get("server_message_full") or detail.get("server_message"))
    elif detail:
        log.warning("LetsFG book refused row=%s: HTTP %s error=%s fields=%s message=%s",
                    booking_id, detail.get("http_status"), detail.get("error"),
                    detail.get("fields"), detail.get("server_message"))
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
        log.warning("LetsFG requires passport details for every booking of user=%s (source=%s)",
                    user, source)
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
        db.record_restriction(slack_user, PROVIDER, db.PASSPORT_REQUIRED, fields=fields,
                              source="multiple_airlines")
        log.warning("LetsFG requires passport details for every booking of user=%s "
                    "(source=multiple_airlines, from history)", slack_user)
        return db.get_restriction(slack_user, PROVIDER, db.PASSPORT_REQUIRED)
    except Exception:  # noqa: BLE001
        return None


def _is_document_field(name: str) -> bool:
    n = str(name).lower()
    return any(w in n for w in ("passport", "nationality", "document", "identity"))


def status(booking_ref: str, booking_id: int | None = None) -> dict:
    if _backend_for(booking_id) == "rest":
        return letsfg_rest.get_booking(booking_ref)
    return get_flight_booking(booking_ref)

REQUIRED_PASSENGER_FIELDS = ("given_name", "family_name", "email")

_mcp_singleton: mcp_client.MCPClient | None = None


def _mcp() -> mcp_client.MCPClient:
    """Lazy MCPClient so importing this module never opens a connection."""
    global _mcp_singleton
    if _mcp_singleton is None:
        _mcp_singleton = mcp_client.MCPClient(
            config.LETSFG_MCP_URL, config.LETSFG_TOKEN
        )
    return _mcp_singleton


def validate_traveller(traveller: dict | None) -> str | None:
    """Return a human-readable error string if traveller is missing required
    fields; else None. Never logs the values themselves."""
    if not traveller or not isinstance(traveller, dict):
        return "traveller profile is missing"
    missing = [f for f in REQUIRED_PASSENGER_FIELDS
               if not str(traveller.get(f, "")).strip()]
    if missing:
        return f"traveller profile is missing required fields: {', '.join(missing)}"
    return None


def _book_args(search_id: str, offer_id: str, traveller: dict) -> dict:
    return {
        "search_id": search_id,
        "offer_id": offer_id,
        "passengers": [
            {
                "given_name": traveller["given_name"],
                "family_name": traveller["family_name"],
                "email": traveller["email"],
                "phone": str(traveller.get("phone", "")).strip(),
            }
        ],
        "contact_email": traveller["email"],
    }


def _looks_like_dev_key_error(msg: str) -> bool:
    low = (msg or "").lower()
    return (
        "developer api key" in low
        or "traveller card" in low
        or "letsfg.co/connect" in low
        or "/developers/api" in low
    )


def book_flight(search_id: str, offer_id: str, traveller: dict) -> dict:
    """Call LetsFG MCP `book_flight` deterministically.

    Returns one of:
      {"status": "need_info", "message": ...}          — traveller incomplete
      {"status": "failed", "message": ...}             — transport/arg error
      {"status": "setup_required", "message": ...}     — Dev key / card missing
      {"status": "awaiting_approval"|..., "booking_ref": ..., "approve_url": ...}
    """
    err = validate_traveller(traveller)
    if err:
        return {"status": "need_info", "message": err}
    if not search_id or not offer_id:
        return {"status": "failed",
                "message": "Missing search_id/offer_id from the pick step."}

    args = _book_args(search_id, offer_id, traveller)
    try:
        result = _mcp().call_tool("book_flight", args)
    except mcp_client.MCPError as e:
        emsg = str(e)
        if _looks_like_dev_key_error(emsg):
            return {
                "status": "setup_required",
                "message": (
                    "LetsFG rejected the booking: the configured LETSFG_TOKEN "
                    "looks like a Developer API key, but `book_flight` on the "
                    "MCP endpoint requires an MCP-scoped token with a traveller "
                    "card connected at letsfg.co/connect. Ask adam@letsfg.co "
                    "for an MCP-scoped token for this workspace."
                ),
            }
        log.warning("book_flight MCP error: %s", emsg[:400])
        return {"status": "failed", "message": emsg[:400]}
    except Exception as e:  # noqa: BLE001
        log.exception("book_flight call failed")
        return {"status": "failed",
                "message": f"{type(e).__name__}: {e}"[:400]}

    data = mcp_client.tool_result_json(result) or {}
    return _map_book_result(data)


def _map_book_result(data: Any) -> dict:
    if not isinstance(data, dict):
        return {"status": "failed", "message": "book_flight returned non-object"}
    status_raw = (data.get("status") or "").lower()
    if status_raw in ("setup_required", "missing_fields", "payment_method_required"):
        return {
            "status": "setup_required",
            "message": data.get("message") or status_raw,
            "details_raw": data,
        }
    booking_ref = (
        data.get("booking_ref") or data.get("id") or data.get("booking_id")
    )
    approve_url = data.get("approve_url") or data.get("approval_url")
    if not booking_ref and status_raw in ("", "failed"):
        return {"status": "failed",
                "message": data.get("message") or "book_flight returned no booking_ref"}
    out_status = (
        "awaiting_approval" if approve_url
        else (status_raw or "booking_started")
    )
    return {
        "status": out_status,
        "booking_ref": booking_ref,
        "approve_url": approve_url,
        "message": data.get("message", ""),
    }


def get_flight_booking(booking_ref: str) -> dict:
    """Call LetsFG MCP `get_flight_booking` deterministically.

    Never reports `completed` without a PNR — a `completed` status with no
    PNR is downgraded to `needs_attention` so the bot does not mislead the
    traveller.
    """
    if not booking_ref:
        return {"status": "needs_attention", "message": "no booking_ref"}
    try:
        result = _mcp().call_tool(
            "get_flight_booking", {"booking_ref": booking_ref}
        )
    except mcp_client.MCPError as e:
        return {"status": "needs_attention", "message": str(e)[:400]}
    except Exception as e:  # noqa: BLE001
        log.exception("get_flight_booking call failed")
        return {"status": "needs_attention",
                "message": f"{type(e).__name__}: {e}"[:400]}

    data = mcp_client.tool_result_json(result) or {}
    return _map_status_result(data)


def _map_status_result(data: Any) -> dict:
    if not isinstance(data, dict):
        return {"status": "needs_attention",
                "message": "get_flight_booking returned non-object"}
    status_raw = (data.get("status") or "").lower() or "booking_in_progress"
    pnr = data.get("pnr") or data.get("booking_reference")
    out_status = status_raw
    # Never claim completed without a PNR.
    if status_raw == "completed" and not pnr:
        out_status = "needs_attention"
    return {
        "status": out_status,
        "pnr": pnr,
        "total": data.get("total") or data.get("total_price"),
        "currency": data.get("currency") or config.CURRENCY,
        "ticket_url": data.get("ticket_url") or data.get("itinerary_url"),
        "message": data.get("message", ""),
    }
