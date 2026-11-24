"""Mirror the legacy bot's ticketed bookings into the ops tables.

The legacy flow (Slack confirm → LetsFG → poll) is unchanged. When it reaches
`completed` with a PNR, `mirror_legacy_row` writes ops_requests → ops_bookings →
ops_ledger debit in one transaction. Safe to call any number of times per row.

Backfill existing rows:  python -m traveldesk.ops.legacy backfill
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
from datetime import UTC, date, datetime
from typing import Any

from traveldesk.booking import flight_scope
from traveldesk.ops import db as ops_db
from traveldesk.ops.models import Booking, BookingStatus
from traveldesk.ops.records import (
    booking_by_ref,
    create_request,
    record_booking,
    upsert_traveler,
    void_booking,
)
from traveldesk.ops.supplier.base import BookingResult
from traveldesk.ops.supplier.letsfg import offer_from_pick
from traveldesk.ops.units import IST, parse_local, to_paise

log = logging.getLogger("traveldesk.ops.legacy")

# Facts from the legacy pick record worth keeping as the parsed request (no PII).
_PARSED_KEYS = (
    "origin",
    "destination",
    "return_from",
    "depart_date",
    "depart_at",
    "airline",
    "airline_name",
    "summary",
    "fare_type",
    "domestic",
    "airports",
    "search_id",
    "offer_id",
    "model",
    "signature",
)


def _details(row: dict) -> dict:
    d = row.get("details")
    if isinstance(d, dict):
        return d
    try:
        return json.loads(d) if d else {}
    except (TypeError, ValueError):
        return {}


def _legacy_created_at(value: Any) -> datetime | None:
    # The bot writes naive *machine-local* time (datetime.now()); astimezone() reads it that way.
    try:
        return datetime.fromisoformat(str(value)).astimezone(UTC)
    except (TypeError, ValueError):
        return None


def _parse_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def mirror_legacy_row(row: dict, profile: dict | None = None, *, pick: dict | None = None) -> int | None:
    """Mirror one legacy `bookings` row. Returns the ops booking id, or None when not mirrorable yet.

    Only rows that are `completed` with a PNR and a LetsFG booking ref are mirrored.
    `pick` is the pick record (route, depart_at…). On completion the bot overwrites
    `details` with the poll status, so the caller passes the pick it read just before;
    without it, `details` is used (route/time may then be unknown).
    `profile` is the traveller profile (traveller_profile.load) — optional; without it the
    traveller row is created from the Slack id alone.
    """
    if row.get("status") != "completed" or not row.get("pnr") or not row.get("booking_ref"):
        return None
    status_details = _details(row)
    d = {**status_details, **(pick or {})}
    if "charged_amount" in status_details:
        d["charged_amount"] = status_details["charged_amount"]
    currency = (d.get("currency") or row.get("currency") or "INR").upper()
    if currency != "INR":
        log.warning("legacy booking %s is in %s, not INR; not mirrored", row.get("id"), currency)
        return None
    amount = row.get("charged_amount")
    if amount in (None, ""):
        amount = d.get("charged_amount") or row.get("price") or d.get("price")
    if amount in (None, ""):
        log.warning("legacy booking %s has no amount; not mirrored", row.get("id"))
        return None

    result = BookingResult(
        state="ticketed",
        supplier_ref=str(row["booking_ref"]),
        pnr=str(row["pnr"]),
        fare_paise=to_paise(amount),
    )
    return _write(row, profile, d, result)


def _write(row: dict, profile: dict | None, d: dict, result: BookingResult) -> int:
    """request → traveller → booking (+ debit as its state requires), in one transaction."""
    origin = str(d.get("origin") or "").upper()
    depart_tz = IST if (d.get("domestic") or origin in flight_scope.INDIA_AIRPORTS) else None
    route = (
        "-".join(d.get("airports") or []) or "-".join(x for x in (origin, d.get("destination")) if x) or "?"
    )

    depart_at = parse_local(d.get("depart_at"), tz=depart_tz) if depart_tz else None

    with ops_db.session_scope() as s:
        req = create_request(
            s,
            slack_user=row.get("slack_user") or "?",
            raw_text=row.get("request") or "",
            parsed={k: d[k] for k in _PARSED_KEYS if k in d},
            legacy_booking_id=int(row["id"]),
            created_at=_legacy_created_at(row.get("created_at")),
        )
        traveler = None
        if row.get("slack_user"):
            p = profile or {}
            name = " ".join(x for x in (p.get("given_name"), p.get("family_name")) if x) or row["slack_user"]
            traveler = upsert_traveler(
                s,
                slack_id=row["slack_user"],
                name=name,
                email=p.get("email"),
                phone=p.get("phone"),
                passport_number=p.get("passport_number"),
                passport_exp=_parse_date(p.get("passport_expiry")),
                meal_pref=p.get("meal_pref"),
                seat_pref=p.get("seat_pref"),
            )
        b: Booking = record_booking(
            s,
            req,
            supplier="letsfg",
            result=result,
            offer=offer_from_pick(d, route=route, depart_at=depart_at, fare_paise=result.fare_paise),
            traveler=traveler,
            route=route,
            depart_at=depart_at,
        )
        return b.id


def _parse_utc(value: Any) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value)) if value else None
    except ValueError:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)) if dt else None


def track_legacy_row(
    row: dict, state: str, profile: dict | None = None, *, pick: dict | None = None, reason: str = ""
) -> int | None:
    """Keep a single (bot) booking in step before its ticket, from the moment LetsFG accepts it.

    state: pending          — LetsFG accepted (and charged): booking + pending debit
           awaiting_payment — LetsFG gave a pay link: booking with the link and its expiry, no debit
           failed/cancelled — ended before a ticket: booking failed/cancelled, any pending debit void
    The ticket itself is recorded by mirror_legacy_row (same booking ref → the same debit, confirmed).
    Returns the ops booking id, or None when there's nothing to track.
    """
    ref = row.get("booking_ref")
    if not ref:
        return None
    if state in ("failed", "cancelled"):
        with ops_db.session_scope() as s:
            b = booking_by_ref(s, "letsfg", str(ref))
            if b is None:
                return None
            void_booking(s, b, status=BookingStatus(state), reason=reason)
            return b.id
    if state not in ("pending", "awaiting_payment"):
        raise ValueError(f"unknown state {state!r}")
    d = {**_details(row), **(pick or {})}
    if (d.get("currency") or row.get("currency") or "INR").upper() != "INR":
        return None
    amount = row.get("price") or d.get("price")
    if amount in (None, ""):
        log.warning("legacy booking %s has no price; not tracked", row.get("id"))
        return None
    result = BookingResult(
        state=state,
        supplier_ref=str(ref),
        fare_paise=to_paise(amount),
        pay_url=(row.get("pay_url") or d.get("pay_url") or d.get("approve_url"))
        if state == "awaiting_payment"
        else None,
        pay_expires_at=_parse_utc(row.get("pay_expires_at")) if state == "awaiting_payment" else None,
    )
    return _write(row, profile, d, result)


def track_legacy_id(row_id: int, state: str, *, pick: dict | None = None, reason: str = "") -> int | None:
    """Called by the bot (guarded) when LetsFG accepts, asks for payment, or ends a booking."""
    from traveldesk.booking import traveller_profile
    from traveldesk.storage import db as legacy_db

    row = legacy_db.get(row_id)
    if not row:
        return None
    profile = None
    if state in ("pending", "awaiting_payment") and row.get("slack_user"):
        profile = traveller_profile.load(row["slack_user"])
    return track_legacy_row(row, state, profile, pick=pick, reason=reason)


def mirror_legacy_id(row_id: int, *, pick: dict | None = None) -> int | None:
    """Called by the bot right after it marks a row completed. Loads the row and profile itself."""
    from traveldesk.booking import traveller_profile
    from traveldesk.storage import db as legacy_db

    row = legacy_db.get(row_id)
    if not row:
        return None
    profile = traveller_profile.load(row["slack_user"]) if row.get("slack_user") else None
    return mirror_legacy_row(row, profile, pick=pick)


def backfill(sqlite_path: str) -> tuple[int, int]:
    """Mirror every completed legacy row. Returns (mirrored, skipped). No profiles: names = Slack ids."""
    conn = sqlite3.connect(sqlite_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = [
            dict(r) for r in conn.execute("SELECT * FROM bookings WHERE status = 'completed' ORDER BY id")
        ]
    finally:
        conn.close()
    done = skipped = 0
    for r in rows:
        if mirror_legacy_row(r) is None:
            skipped += 1
        else:
            done += 1
    return done, skipped


def main(argv: list[str] | None = None) -> None:
    from traveldesk.ops.settings import get_settings

    ap = argparse.ArgumentParser(prog="python -m traveldesk.ops.legacy")
    ap.add_argument("command", choices=["backfill"])
    args = ap.parse_args(argv)
    if args.command == "backfill":
        url = get_settings().database_url
        if not url.startswith("sqlite:///"):
            raise SystemExit("backfill reads the legacy SQLite file; set DATABASE_PATH")
        done, skipped = backfill(url.removeprefix("sqlite:///"))
        print(f"mirrored {done} completed booking(s), skipped {skipped}")


if __name__ == "__main__":
    main()
