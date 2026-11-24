"""Phase 4 — split-roster group booking.

    /traveldesk DEL to BOM 1 Dec morning for @asha @ravi @meera

1. A request that @mentions someone other than the requester (or two or more people) is a
   group request. Only travel admins (TRAVEL_ADMIN_SLACK_IDS) may make one.
2. The bot picks one flight (same picker as single bookings), then checks every traveller:
   set up in travellers.json · Slack name matches the profile · booking details complete
   (international: passport present and valid ≥ 6 months after the last flight) · fare
   within MAX_PRICE.
3. One card to the requester: "7 ready / 3 need action". One [Book 7 travellers] click books
   each ready traveller as a separate booking (one passenger each), so one failure never
   blocks the rest. Availability ("seat available") is the booking call itself: a sold-out
   or expired fare comes back as that traveller's reason.
4. The card becomes "7 booked / 3 need action", each exception with its reason and a
   [Fix & retry] button that re-checks and books only that traveller.
5. LetsFG tickets asynchronously: accepted bookings are `pending` until `poll_pending`
   (bot thread + scheduler job) sees the PNR; then the booking is recorded
   (requests → bookings → ledger debit) and the traveller gets their ticket by DM.

DRY_RUN=true: everything up to the supplier call runs; nothing is booked.
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select, update

from traveldesk.booking import flight_scope
from traveldesk.ops import db as ops_db
from traveldesk.ops.flow import book_one
from traveldesk.ops.models import Booking, BookingStatus, Request, RequestStatus, RosterMember, RosterStatus
from traveldesk.ops.records import (
    booking_by_ref,
    create_request,
    record_booking,
    upsert_traveler,
    void_booking,
)
from traveldesk.ops.supplier.base import (
    Supplier,
    SupplierError,
    TravelerDetails,
    new_trace_id,
    offer_from_json,
    offer_to_json,
)
from traveldesk.ops.supplier.letsfg import offer_from_pick
from traveldesk.ops.units import IST, fmt_ist, format_inr, parse_local, to_paise, utcnow

log = logging.getLogger("traveldesk.ops.group")

CONFIRM_ACTION = "ops_group_confirm"
CANCEL_ACTION = "ops_group_cancel"
RETRY_ACTION = "ops_group_retry"
APPROVE_ACTION = "ops_group_book_at"  # [Book at ₹X] after a price change
MAX_GROUP = 20

_MENTION = re.compile(r"<@([UW][A-Z0-9]+)(?:\|[^>]*)?>")
_PLAIN_AT = re.compile(r"(?<![\w<@.])@([A-Za-z][\w.\-]+)")
_DOCUMENT_FIELDS = ("nationality", "passport_number", "passport_country", "passport_expiry", "passport_issue")
# Pick facts kept on the request (no PII).
_PICK_KEYS = ("summary", "price", "currency", "depart_date", "depart_at", "last_flight_date", "domestic",
              "airports", "origin", "destination", "airline", "airline_name", "required_fields")  # fmt: skip


# --- parsing --------------------------------------------------------------------


@dataclass(frozen=True)
class Roster:
    slack_ids: list[str]
    text: str  # the request without the mentions (what the flight picker sees)
    plain_names: list[str]  # "@asha" typed without Slack turning it into a mention


def parse_roster(text: str) -> Roster:
    ids = list(dict.fromkeys(_MENTION.findall(text or "")))
    rest = _MENTION.sub(" ", text or "")
    plain = _PLAIN_AT.findall(rest)
    return Roster(ids, re.sub(r"\s+", " ", rest).strip(" ,"), plain)


def is_group_request(text: str, requester: str) -> bool:
    r = parse_roster(text)
    return (
        bool(set(r.slack_ids) - {requester})
        or len(r.slack_ids) >= 2
        or (not r.slack_ids and bool(r.plain_names))
    )


# --- policy, dependencies -------------------------------------------------------


@dataclass(frozen=True)
class Policy:
    admins: frozenset[str]
    cap_paise: int | None
    dry_run: bool
    passport_months: int = 6
    offer_mode: str = "per_traveller"  # GROUP_OFFER_MODE: per_traveller | shared
    tolerance_pct: float = 5.0  # PRICE_TOLERANCE_PCT
    ticket_timeout: timedelta = timedelta(hours=2)  # TICKET_TIMEOUT


@dataclass
class Deps:
    supplier: Supplier
    pick: Callable[[str, dict], dict]  # (request text, requester public profile) → bot pick record
    load_profile: Callable[[str], dict | None]
    validate: Callable[..., Any]  # traveller_profile.validate
    saved_fields: Callable[..., tuple[dict, list]]  # traveller_profile.saved_fields
    public_view: Callable[[dict | None], dict]
    spawn: Callable[[Callable[[], None]], None] = lambda fn: threading.Thread(target=fn, daemon=True).start()


def bot_policy() -> Policy:
    from traveldesk import config
    from traveldesk.booking import traveller_profile
    from traveldesk.ops.settings import get_settings

    cfg = get_settings()
    return Policy(
        admins=frozenset(config.TRAVEL_ADMINS),
        cap_paise=to_paise(config.MAX_PRICE) if config.MAX_PRICE else None,
        dry_run=config.DRY_RUN,
        passport_months=traveller_profile.PASSPORT_VALIDITY_MONTHS,
        offer_mode=cfg.group_offer_mode,
        tolerance_pct=cfg.price_tolerance_pct,
        ticket_timeout=cfg.ticket_timeout,
    )


def bot_deps() -> Deps:
    from traveldesk import agents
    from traveldesk.booking import traveller_profile
    from traveldesk.ops.supplier import get_supplier

    agent = agents.get_agent()
    return Deps(
        supplier=get_supplier(),
        pick=lambda request, traveller: agent.pick(request, traveller, None),
        load_profile=traveller_profile.load,
        validate=traveller_profile.validate,
        saved_fields=traveller_profile.saved_fields,
        public_view=traveller_profile.public_view,
    )


# --- checks -------------------------------------------------------------------------


def _tokens(*names: str | None) -> set[str]:
    return {t for n in names if n for t in re.findall(r"[a-z]+", n.lower())}


def _slack_names(client: Any, slack_id: str) -> tuple[str, ...]:
    try:
        u = client.users_info(user=slack_id)["user"]
    except Exception:  # noqa: BLE001 — no users:read / API error: skip the name check
        return ()
    p = u.get("profile") or {}
    return tuple(n for n in (u.get("real_name"), p.get("real_name"), p.get("display_name")) if n)


def name_mismatch(profile: dict, slack_names: tuple[str, ...]) -> str | None:
    """The Slack account's name must share the profile's given or family name (catches wrong IDs)."""
    if not slack_names:
        return None
    given = _tokens(profile.get("given_name"))
    family = _tokens(profile.get("family_name"))
    if (given | family) & _tokens(*slack_names):
        return None
    shown = f"{profile.get('given_name', '')} {profile.get('family_name', '')}".strip()
    return f"Slack name “{slack_names[0]}” doesn't match travellers.json name “{shown}” — check the Slack ID"


def check_member(
    profile: dict | None,
    slack_names: tuple[str, ...],
    pick: dict,
    fare_paise: int | None,
    policy: Policy,
    deps: Deps,
) -> tuple[str | None, dict | None]:
    """(reason, passenger). reason=None → bookable, passenger = the fields to send. No values in reasons.

    fare_paise=None skips the cap check (at booking time the re-pricing rules decide)."""
    if profile is None:
        return "not set up in travellers.json", None
    if reason := name_mismatch(profile, slack_names):
        return reason, None
    domestic = bool(pick.get("domestic"))
    trip = {"travel_date": pick.get("depart_date"), "last_flight_date": pick.get("last_flight_date")}
    months = 0 if domestic else policy.passport_months
    res = deps.validate(profile, for_booking=True, domestic=domestic, passport_months=months, **trip)
    if not res.ok:
        return "details incomplete — " + res.summary(), None
    passenger = dict(res.cleaned)
    if domestic:
        passenger = {k: v for k, v in passenger.items() if k not in _DOCUMENT_FIELDS}
    asked = list(pick.get("required_fields") or [])
    if asked:
        have, need = deps.saved_fields(profile, asked, passport_months=months, **trip)
        if need:
            return "LetsFG also needs: " + ", ".join(need), None
        passenger.update(have)
    if fare_paise is not None and policy.cap_paise is not None and fare_paise > policy.cap_paise:
        return f"fare {format_inr(fare_paise)} is over the cap {format_inr(policy.cap_paise)}", None
    return None, passenger


# --- re-pricing ------------------------------------------------------------------------


def price_ok(shown: int, new: int, *, cap: int | None, tolerance_pct: float) -> bool:
    """Book at `new` without asking? Same or lower → yes. Higher → only within the tolerance and the cap."""
    if new <= shown:
        return True
    if cap is not None and new > cap:
        return False
    return Decimal(new - shown) * 100 <= Decimal(shown) * Decimal(str(tolerance_pct))


def price_change_text(shown: int, new: int, cap: int | None) -> str:
    pct = (Decimal(new - shown) * 100 / Decimal(shown)).quantize(Decimal("0.1")) if shown else Decimal(0)
    over = f", over the cap {format_inr(cap)}" if cap is not None and new > cap else ""
    return (
        f"fare {format_inr(shown)} → {format_inr(new)} ({'+' if pct >= 0 else ''}{pct.normalize():f}%{over})"
    )


# --- start: parse → authorize → pick → check everyone → review card ------------------


def start(client: Any, requester: str, text: str, channel: str, *, deps: Deps, policy: Policy) -> bool:
    """Checks on the request itself, then the search on a background thread. False = refused."""
    r = parse_roster(text)
    if not r.slack_ids and r.plain_names:
        client.chat_postMessage(
            channel=channel,
            text="Mention each traveller with @ (pick them from Slack's list) so I get their exact Slack account. "
            "If you did, ask the Slack admin to turn on *Escape channels, users, and links* for /traveldesk.",
        )
        return False
    if requester not in policy.admins:
        admins = ", ".join(f"<@{a}>" for a in sorted(policy.admins)) or "a travel admin"
        client.chat_postMessage(
            channel=channel,
            text=f"Only travel admins can book for other people — ask {admins}. "
            "To book just for yourself, send the request without @mentions.",
        )
        return False
    if len(r.slack_ids) > MAX_GROUP:
        client.chat_postMessage(
            channel=channel,
            text=f"That's {len(r.slack_ids)} travellers; the limit is {MAX_GROUP} per request.",
        )
        return False
    first = client.chat_postMessage(
        channel=channel,
        text=f":busy_buses: *Group of {len(r.slack_ids)}:* {r.text}\nSearching flights… (2–3 minutes)",
    )
    deps.spawn(
        lambda: run_search(client, requester, text, channel, first.get("ts"), deps=deps, policy=policy)
    )
    return True


def run_search(
    client: Any, requester: str, text: str, channel: str, thread_ts: str | None, *, deps: Deps, policy: Policy
) -> int | None:
    r = parse_roster(text)
    trace_id = new_trace_id()
    with ops_db.session_scope() as s:
        rid = create_request(s, slack_user=requester, raw_text=text, trace_id=trace_id).id

    def fail(msg: str) -> None:
        _set_request_status(rid, RequestStatus.FAILED)
        client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=msg)

    me = deps.load_profile(requester)
    try:
        pick = deps.pick(r.text, deps.public_view(me) if me else {})
    except Exception as e:  # noqa: BLE001 — surfaced to the requester
        log.exception("group pick failed for request %s", rid)
        fail(f":x: Search failed: {e}")
        return rid
    if pick.get("status") != "picked":
        fail(f":warning: Not booked ({pick.get('status')}). {pick.get('message', '')}")
        return rid
    if (pick.get("currency") or "INR").upper() != "INR":
        fail(f":warning: The fare came back in {pick.get('currency')}; group booking works in INR only.")
        return rid

    fare = to_paise(pick["price"])
    route = "-".join(pick.get("airports") or []) or f"{pick.get('origin')}-{pick.get('destination')}"
    india = pick.get("domestic") or str(pick.get("origin") or "").upper() in flight_scope.INDIA_AIRPORTS
    offer = offer_from_pick(
        pick,
        route=route,
        depart_at=parse_local(pick.get("depart_at"), tz=IST) if india else None,
        fare_paise=fare,
    )
    if offer is None or not offer.search_id:
        fail(":warning: The picked flight has no bookable details; try again.")
        return rid

    with ops_db.session_scope() as s:
        req = s.get(Request, rid)
        req.parsed_json = {
            "group": True,
            "pick": {k: pick[k] for k in _PICK_KEYS if k in pick},
            "offer": offer_to_json(offer),
            "fare_paise": fare,
            "channel": channel,
        }
    for sid in r.slack_ids:
        profile = deps.load_profile(sid)
        reason, _ = check_member(profile, _slack_names(client, sid), pick, fare, policy, deps)
        with ops_db.session_scope() as s:
            trav = _upsert_from_profile(s, sid, profile) if profile else None
            s.add(
                RosterMember(
                    request_id=rid,
                    slack_id=sid,
                    traveler_id=trav.id if trav else None,
                    status=RosterStatus.NEEDS_ACTION if reason else RosterStatus.READY,
                    reason=reason,
                )
            )

    text_, blocks = render(rid, policy)
    resp = client.chat_postMessage(channel=channel, text=text_, blocks=blocks)
    with ops_db.session_scope() as s:
        req = s.get(Request, rid)
        req.parsed_json = {**req.parsed_json, "card_ts": resp.get("ts")}
    return rid


def _upsert_from_profile(s, slack_id: str, p: dict):
    from datetime import date

    try:
        exp = date.fromisoformat(str(p.get("passport_expiry"))[:10]) if p.get("passport_expiry") else None
    except ValueError:
        exp = None
    name = " ".join(x for x in (p.get("given_name"), p.get("family_name")) if x) or slack_id
    return upsert_traveler(
        s, slack_id=slack_id, name=name, email=p.get("email"), phone=p.get("phone"),
        passport_number=p.get("passport_number"), passport_exp=exp,
        meal_pref=p.get("meal_pref"), seat_pref=p.get("seat_pref"),
    )  # fmt: skip


# --- confirm / retry / cancel ---------------------------------------------------------


def _allowed(req: Request, actor: str, policy: Policy) -> bool:
    return actor == req.slack_user or actor in policy.admins


def _set_request_status(rid: int, status: RequestStatus) -> None:
    with ops_db.session_scope() as s:
        s.get(Request, rid).status = status


def _claim_request(rid: int, frm: RequestStatus, to: RequestStatus) -> bool:
    with ops_db.session_scope() as s:
        return (
            s.execute(
                update(Request).where(Request.id == rid, Request.status == frm).values(status=to)
            ).rowcount
            == 1
        )


def _claim_member(mid: int, frm: tuple[RosterStatus, ...], *, bump_attempt: bool = False) -> bool:
    values: dict[str, Any] = {"status": RosterStatus.BOOKING, "updated_at": utcnow()}
    if bump_attempt:
        values["attempt"] = RosterMember.attempt + 1
    with ops_db.session_scope() as s:
        return (
            s.execute(
                update(RosterMember)
                .where(RosterMember.id == mid, RosterMember.status.in_(frm))
                .values(**values)
            ).rowcount
            == 1
        )


def confirm(client: Any, rid: int, actor: str, *, deps: Deps, policy: Policy) -> bool:
    """[Book N travellers]: book every ready traveller. False = not allowed / already confirmed."""
    with ops_db.session_scope() as s:
        req = s.get(Request, rid)
        if req is None or not _allowed(req, actor, policy):
            return False
    if not _claim_request(rid, RequestStatus.RECEIVED, RequestStatus.BOOKING):
        return False  # double click, or cancelled
    with ops_db.session_scope() as s:
        ready = [
            m.id
            for m in s.scalars(
                select(RosterMember).where(
                    RosterMember.request_id == rid, RosterMember.status == RosterStatus.READY
                )
            )
        ]
    pricing = "fresh" if policy.offer_mode == "per_traveller" else "none"
    for mid in ready:
        _book_member(
            client, mid, actor, deps=deps, policy=policy, claim_from=(RosterStatus.READY,), pricing=pricing
        )
    settle(client, rid, policy)
    return True


def retry(client: Any, rid: int, slack_id: str, actor: str, *, deps: Deps, policy: Policy) -> bool:
    """[Fix & retry]: re-check and book only this traveller. False = not allowed / not retryable now."""
    with ops_db.session_scope() as s:
        req = s.get(Request, rid)
        m = s.scalar(
            select(RosterMember).where(RosterMember.request_id == rid, RosterMember.slack_id == slack_id)
        )
        if (
            req is None
            or m is None
            or not _allowed(req, actor, policy)
            or req.status is RequestStatus.RECEIVED
        ):
            return False
        mid = m.id
    ok = _book_member(
        client,
        mid,
        actor,
        deps=deps,
        policy=policy,
        claim_from=(RosterStatus.NEEDS_ACTION,),
        bump_attempt=True,
        pricing="retry",
    )
    settle(client, rid, policy)
    return ok


def approve(
    client: Any, rid: int, slack_id: str, actor: str, fare_paise: int, *, deps: Deps, policy: Policy
) -> bool:
    """[Book at ₹X]: the admin accepted the new fare X. Re-checks once more and books only at ≤ X;
    if it moved again, the card shows the new fare and a new button instead."""
    with ops_db.session_scope() as s:
        req = s.get(Request, rid)
        m = s.scalar(
            select(RosterMember).where(RosterMember.request_id == rid, RosterMember.slack_id == slack_id)
        )
        if req is None or m is None or not _allowed(req, actor, policy):
            return False
        if m.status is not RosterStatus.PRICE_CHANGED or m.fare_paise != fare_paise:
            return False  # stale button (already booked, or a newer price is on the card)
        mid = m.id
    ok = _book_member(
        client, mid, actor, deps=deps, policy=policy,
        claim_from=(RosterStatus.PRICE_CHANGED,), bump_attempt=True, pricing="approved",
    )  # fmt: skip
    settle(client, rid, policy)
    return ok


def cancel(client: Any, rid: int, actor: str, *, policy: Policy) -> bool:
    with ops_db.session_scope() as s:
        req = s.get(Request, rid)
        if req is None or not _allowed(req, actor, policy):
            return False
    if not _claim_request(rid, RequestStatus.RECEIVED, RequestStatus.CANCELLED):
        return False
    with ops_db.session_scope() as s:
        s.execute(
            update(RosterMember).where(RosterMember.request_id == rid).values(status=RosterStatus.CANCELLED)
        )
    _update_card(client, rid, policy)
    return True


def _book_member(
    client,
    mid: int,
    actor: str,
    *,
    deps: Deps,
    policy: Policy,
    claim_from,
    bump_attempt=False,
    pricing="none",
) -> bool:
    """Check, (re-)price and book one traveller.

    pricing: none     — book the picked offer as is (GROUP_OFFER_MODE=shared, first confirm)
             fresh    — re-search the same flight, book a fresh offer (per_traveller, first confirm)
             retry    — [Fix & retry]: re-search first; a stale fare is never booked silently
             approved — [Book at ₹X]: re-search; book only at ≤ X
    Price rules: same/lower → book; higher within PRICE_TOLERANCE_PCT and MAX_PRICE → book (the card
    shows the new fare); otherwise → price_changed with a [Book at ₹X] button.
    """
    if not _claim_member(mid, claim_from, bump_attempt=bump_attempt):
        return False
    with ops_db.session_scope() as s:
        m = s.get(RosterMember, mid)
        req = m.request
        rid, sid, attempt, trace_id = req.id, m.slack_id, m.attempt, req.trace_id
        parsed = req.parsed_json or {}
        shown = m.fare_paise or parsed["fare_paise"]  # the fare the admin last saw for this traveller
    pick, offer = parsed["pick"], offer_from_json(parsed["offer"])

    def finish(status: RosterStatus, **fields) -> None:
        with ops_db.session_scope() as s:
            m = s.get(RosterMember, mid)
            m.status, m.reason = status, fields.pop("reason", None)
            for k, v in fields.items():
                setattr(m, k, v)

    try:
        profile = deps.load_profile(sid)  # fresh: the fix may have been made a minute ago
        reason, passenger = check_member(
            profile, _slack_names(client, sid), pick, None if pricing != "none" else shown, policy, deps
        )
        if reason:
            finish(RosterStatus.NEEDS_ACTION, reason=reason)
            return True
        with ops_db.session_scope() as s:
            trav_id = _upsert_from_profile(s, sid, profile).id
            s.get(RosterMember, mid).traveler_id = trav_id
        if policy.dry_run:
            finish(RosterStatus.DRY_RUN)
            return True
        book_offer = offer
        if pricing != "none":
            try:
                quote = deps.supplier.price_check(offer, trace_id=trace_id or new_trace_id(), exact=True)
            except SupplierError as e:
                finish(RosterStatus.NEEDS_ACTION, reason=f"couldn't re-check the fare ({e.code}) — try again")
                return True
            if not quote.available or quote.fare_paise is None:
                why = (
                    "flight no longer available"
                    if pricing == "fresh"
                    else "fare expired — send a new request"
                )
                finish(RosterStatus.NEEDS_ACTION, reason=why)
                return True
            new = quote.fare_paise
            fresh = quote.offer or replace(offer, fare_paise=new)
            tolerance = 0.0 if pricing == "approved" else policy.tolerance_pct
            if not price_ok(shown, new, cap=policy.cap_paise, tolerance_pct=tolerance):
                finish(
                    RosterStatus.PRICE_CHANGED,
                    reason=price_change_text(shown, new, policy.cap_paise),
                    fare_paise=new,
                    quote_offer_json=offer_to_json(fresh),
                )
                return True
            book_offer = fresh
        p = dict(passenger)
        details = TravelerDetails(
            slack_id=sid,
            given_name=p.pop("given_name"),
            family_name=p.pop("family_name"),
            email=p.pop("email"),
            phone=p.pop("phone", ""),
            extra=p,
        )
        out = book_one(
            deps.supplier,
            request_id=rid,
            traveler_id=trav_id,
            offer=book_offer,
            details=details,
            trace_id=trace_id,
            attempt=attempt,
        )
    except Exception as e:  # noqa: BLE001 — one traveller's failure never stops the others
        log.exception("group booking failed: request %s member %s", rid, sid)
        finish(RosterStatus.NEEDS_ACTION, reason=f"booking error: {type(e).__name__}")
        return True

    booked_fare = (
        out.result.fare_paise if out.result and out.result.fare_paise is not None else book_offer.fare_paise
    )
    if out.state in ("ticketed", "held"):
        finish(RosterStatus.BOOKED, booking_id=out.booking_id, supplier_ref=None, pay_url=None,
               fare_paise=booked_fare, quote_offer_json=None)  # fmt: skip
        _dm_ticket(client, out.booking_id, sid, actor)
    elif out.state in ("pending", "awaiting_payment"):
        finish(RosterStatus.PENDING, booking_id=out.booking_id, supplier_ref=out.result.supplier_ref,
               pay_url=out.result.pay_url, fare_paise=booked_fare, quote_offer_json=None)  # fmt: skip
    else:
        finish(RosterStatus.NEEDS_ACTION, reason=out.error or "the supplier refused the booking")
    return True


def _dm_ticket(client, booking_id: int | None, slack_id: str, booked_by: str | None) -> None:
    if client is None or booking_id is None:
        return
    with ops_db.session_scope() as s:
        b = s.get(Booking, booking_id)
        text = (
            f":ticket: *Your ticket is booked* — PNR *{b.pnr or '—'}*\n"
            f"{b.route} · {fmt_ist(b.depart_at)} IST" + (f" · booked by <@{booked_by}>" if booked_by else "")
        )
    try:
        client.chat_postMessage(channel=client.conversations_open(users=slack_id)["channel"]["id"], text=text)
    except Exception:  # noqa: BLE001 — the booking stands; the card still shows the PNR
        log.exception("ticket DM to %s failed", slack_id)


# --- pending → ticketed ------------------------------------------------------------------


def poll_pending(
    supplier: Supplier,
    client: Any = None,
    *,
    now: datetime | None = None,
    request_id: int | None = None,
    ticket_timeout: timedelta = timedelta(hours=2),
) -> int:
    """Check every pending traveller with the supplier. Returns how many changed.

    Idempotent (safe after a restart, or alongside the scheduler job): the booking and its debit are
    keyed by the supplier reference, so re-recording the same state writes nothing new.
      ticketed/held      → booking ticketed, debit confirmed, traveller booked + ticket DM
      pending            → (pay link just paid) pending debit written; after TICKET_TIMEOUT → needs action,
                           debit stays pending (the digest's "Charged, no ticket" bucket)
      awaiting_payment   → pay link expired → booking failed, nothing charged, needs action
      failed / cancelled → booking failed/cancelled, debit void, needs action
    """
    now = now or utcnow()
    with ops_db.session_scope() as s:
        q = select(RosterMember.id).where(
            RosterMember.status == RosterStatus.PENDING, RosterMember.supplier_ref.is_not(None)
        )
        if request_id is not None:
            q = q.where(RosterMember.request_id == request_id)
        ids = list(s.scalars(q))
    changed, touched = 0, set()
    for mid in ids:
        with ops_db.session_scope() as s:
            m = s.get(RosterMember, mid)
            ref, rid, sid, trace_id = m.supplier_ref, m.request_id, m.slack_id, m.request.trace_id
        try:
            res = supplier.get_booking(ref, trace_id=trace_id or new_trace_id())
        except SupplierError:
            continue
        ticketed = None
        with ops_db.session_scope() as s:
            m = s.get(RosterMember, mid)
            if m.status is not RosterStatus.PENDING:
                continue
            req = m.request
            existing = booking_by_ref(s, supplier.name, ref)
            # Known booking: keep its fare (the traveller's own fresh fare); else fall back to the picked offer.
            offer = None if existing else offer_from_json(req.parsed_json["offer"])
            if res.state in ("ticketed", "held", "pending", "awaiting_payment"):
                b = record_booking(
                    s, req, supplier=supplier.name, result=res, offer=offer, traveler=m.traveler
                )
                m.booking_id = b.id
                if b.status in (BookingStatus.TICKETED, BookingStatus.HELD):
                    m.status, m.reason, m.pay_url, m.fare_paise = (
                        RosterStatus.BOOKED,
                        None,
                        None,
                        b.fare_paise,
                    )
                    ticketed = b.id
                elif b.status is BookingStatus.AWAITING_PAYMENT:
                    if b.pay_expires_at is None or now < b.pay_expires_at:
                        continue
                    void_booking(s, b, status=BookingStatus.FAILED, reason="payment link expired")
                    m.status, m.pay_url = RosterStatus.NEEDS_ACTION, None
                    m.reason = "payment link expired — nothing was charged"
                elif b.status is BookingStatus.PENDING:
                    paid_now = m.pay_url is not None
                    m.pay_url = None
                    if now - (b.status_changed_at or now) <= ticket_timeout:
                        if not paid_now:
                            continue
                    else:
                        hours = ticket_timeout.total_seconds() / 3600
                        m.status = RosterStatus.NEEDS_ACTION
                        m.reason = (
                            f"charged but not ticketed after {hours:g}h — check the supplier (ref {ref})"
                        )
                else:
                    continue
            elif res.state in ("failed", "cancelled"):
                if existing is not None:
                    status = BookingStatus.CANCELLED if res.state == "cancelled" else BookingStatus.FAILED
                    void_booking(s, existing, status=status, reason=res.message)
                m.status, m.pay_url = RosterStatus.NEEDS_ACTION, None
                m.reason = res.message or f"booking {res.state} at the supplier"
            else:
                continue
        changed += 1
        touched.add(rid)
        if ticketed:
            _dm_ticket(client, ticketed, sid, None)
    for rid in touched:
        settle(client, rid, None)
    return changed


# --- request status + card ------------------------------------------------------------------


def settle(client: Any, rid: int, policy: Policy | None) -> RequestStatus:
    with ops_db.session_scope() as s:
        req = s.get(Request, rid)
        statuses = [m.status for m in s.scalars(select(RosterMember).where(RosterMember.request_id == rid))]
        done = {RosterStatus.BOOKED, RosterStatus.DRY_RUN}
        if req.status in (RequestStatus.RECEIVED, RequestStatus.CANCELLED):
            new = req.status
        elif any(x in (RosterStatus.BOOKING, RosterStatus.PENDING) for x in statuses):
            new = RequestStatus.BOOKING
        elif statuses and all(x in done for x in statuses):
            new = RequestStatus.BOOKED
        elif any(x in done for x in statuses):
            new = RequestStatus.PARTIAL
        else:
            new = RequestStatus.FAILED
        req.status = new
    _update_card(client, rid, policy)
    return new


def _update_card(client: Any, rid: int, policy: Policy | None) -> None:
    with ops_db.session_scope() as s:
        parsed = s.get(Request, rid).parsed_json or {}
    if client is None or not parsed.get("card_ts"):
        return
    text, blocks = render(rid, policy)
    try:
        client.chat_update(channel=parsed["channel"], ts=parsed["card_ts"], text=text, blocks=blocks)
    except Exception:  # noqa: BLE001 — state is in the DB; the next update redraws it
        log.exception("group card update failed for request %s", rid)


def _sec(text: str, accessory: dict | None = None) -> dict:
    b: dict[str, Any] = {"type": "section", "text": {"type": "mrkdwn", "text": text}}
    if accessory:
        b["accessory"] = accessory
    return b


def render(rid: int, policy: Policy | None) -> tuple[str, list[dict]]:
    with ops_db.session_scope() as s:
        req = s.get(Request, rid)
        parsed = req.parsed_json or {}
        members = s.scalars(
            select(RosterMember).where(RosterMember.request_id == rid).order_by(RosterMember.id)
        ).all()
        pnrs = {m.id: (m.booking.pnr if m.booking else None) for m in members}
        status = req.status
    pick, fare = parsed.get("pick", {}), parsed.get("fare_paise", 0)
    by = {st: [m for m in members if m.status is st] for st in RosterStatus}
    need = by[RosterStatus.NEEDS_ACTION]
    repriced = by[RosterStatus.PRICE_CHANGED]

    def fare_note(m: RosterMember) -> str:
        return (
            f" · {format_inr(m.fare_paise)} (was {format_inr(fare)})"
            if m.fare_paise and m.fare_paise != fare
            else ""
        )

    head = _sec(
        f"*Group booking* · {pick.get('summary', '')}\n{format_inr(fare)} per person · {len(members)} travellers"
    )

    if status is RequestStatus.CANCELLED:
        return "Group booking cancelled", [head, _sec(":no_entry_sign: Cancelled — nothing was booked.")]

    if status is RequestStatus.RECEIVED:  # review, before Confirm
        ready = by[RosterStatus.READY]
        blocks = [head]
        if ready:
            blocks.append(
                _sec(
                    f":white_check_mark: *Ready ({len(ready)}):* "
                    + ", ".join(f"<@{m.slack_id}>" for m in ready)
                )
            )
        if need:
            blocks.append(
                _sec(
                    f":warning: *Need action ({len(need)}):*\n"
                    + "\n".join(f"• <@{m.slack_id}> — {m.reason}" for m in need)
                )
            )
        elements = []
        if ready:
            word = "traveller" if len(ready) == 1 else "travellers"
            elements.append({"type": "button", "style": "primary", "action_id": CONFIRM_ACTION, "value": str(rid),
                             "text": {"type": "plain_text", "text": f"Book {len(ready)} {word} · {format_inr(fare * len(ready))}"}})  # fmt: skip
        elements.append(
            {
                "type": "button",
                "action_id": CANCEL_ACTION,
                "value": str(rid),
                "text": {"type": "plain_text", "text": "Cancel"},
            }
        )
        blocks.append({"type": "actions", "block_id": f"ops_group_{rid}", "elements": elements})
        note = ("DRY_RUN is on: nothing will really be booked." if policy and policy.dry_run
                else "Each traveller is booked separately. This is a real booking.")  # fmt: skip
        if need:
            note += " Travellers who need action can be retried after booking."
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": note}]})
        return f"Group booking: {len(ready)} ready / {len(need)} need action", blocks

    booked, pending, dry = by[RosterStatus.BOOKED], by[RosterStatus.PENDING], by[RosterStatus.DRY_RUN]
    in_flight = by[RosterStatus.BOOKING]
    if dry and not booked and not pending:
        title = f"{len(dry)} would be booked (dry run) / {len(need) + len(repriced)} need action"
    else:
        title = f"{len(booked) + len(pending)} booked / {len(need) + len(repriced)} need action"
        if pending:
            title += f" · {len(pending)} ticket{'s' if len(pending) > 1 else ''} on the way"
    blocks = [head, _sec(f"*{title}*")]
    lines = [f"• <@{m.slack_id}> — PNR *{pnrs[m.id] or '—'}*{fare_note(m)}" for m in booked]
    lines += [
        (f"• <@{m.slack_id}> — <{m.pay_url}|payment link> sent, not paid yet{fare_note(m)}" if m.pay_url
         else f"• <@{m.slack_id}> — booked, ticket on the way{fare_note(m)}")
        for m in pending
    ]  # fmt: skip
    lines += [f"• <@{m.slack_id}> — dry run: would book" for m in dry]
    lines += [f"• <@{m.slack_id}> — booking…" for m in in_flight]
    if lines:
        blocks.append(_sec("\n".join(lines)))
    for m in need:
        blocks.append(_sec(f":warning: <@{m.slack_id}> — {m.reason}",
                           {"type": "button", "action_id": RETRY_ACTION, "value": f"{rid}:{m.slack_id}",
                            "text": {"type": "plain_text", "text": "Fix & retry"}}))  # fmt: skip
    for m in repriced:
        blocks.append(_sec(f":chart_with_upwards_trend: <@{m.slack_id}> — {m.reason}. Not booked.",
                           {"type": "button", "action_id": APPROVE_ACTION, "value": f"{rid}:{m.slack_id}:{m.fare_paise}",
                            "text": {"type": "plain_text", "text": f"Book at {format_inr(m.fare_paise)}"}}))  # fmt: skip
    return f"Group booking: {title}", blocks


# --- Slack wiring (bot process) ---------------------------------------------------------------


def register(
    app: Any, deps_factory: Callable[[], Deps] = bot_deps, policy_factory: Callable[[], Policy] = bot_policy
) -> None:
    """Button handlers. Work runs on a thread: Slack needs the ack within 3 s, bookings take longer."""

    def run(fn: Callable[[], None]) -> None:
        threading.Thread(target=fn, daemon=True).start()

    @app.action(CONFIRM_ACTION)
    def _confirm(ack, body, client):
        ack()
        rid, actor = int(body["actions"][0]["value"]), body["user"]["id"]

        def work():
            deps, policy = deps_factory(), policy_factory()
            if confirm(client, rid, actor, deps=deps, policy=policy) and not policy.dry_run:
                watch(client, rid, deps, until=policy.ticket_timeout)

        run(work)

    @app.action(RETRY_ACTION)
    def _retry(ack, body, client):
        ack()
        rid, _, sid = body["actions"][0]["value"].partition(":")
        actor = body["user"]["id"]

        def work():
            deps, policy = deps_factory(), policy_factory()
            if retry(client, int(rid), sid, actor, deps=deps, policy=policy) and not policy.dry_run:
                watch(client, int(rid), deps, until=policy.ticket_timeout)

        run(work)

    @app.action(APPROVE_ACTION)
    def _approve(ack, body, client):
        ack()
        rid, sid, fare = body["actions"][0]["value"].split(":")
        actor = body["user"]["id"]

        def work():
            deps, policy = deps_factory(), policy_factory()
            ok = approve(client, int(rid), sid, actor, int(fare), deps=deps, policy=policy)
            if ok and not policy.dry_run:
                watch(client, int(rid), deps, until=policy.ticket_timeout)

        run(work)

    @app.action(CANCEL_ACTION)
    def _cancel(ack, body, client):
        ack()
        cancel(client, int(body["actions"][0]["value"]), body["user"]["id"], policy=policy_factory())


def watch(
    client: Any, rid: int, deps: Deps, *, every: float | None = None, until: timedelta = timedelta(hours=2)
) -> None:
    """Poll this request's pending travellers until none are left (the scheduler job covers restarts)."""
    import time

    from traveldesk import config

    every = every if every is not None else config.POLL_SECONDS
    deadline = utcnow() + until + timedelta(minutes=5)
    while utcnow() < deadline:
        with ops_db.session_scope() as s:
            left = s.scalar(
                select(RosterMember.id)
                .where(RosterMember.request_id == rid, RosterMember.status == RosterStatus.PENDING)
                .limit(1)
            )
        if left is None:
            return
        time.sleep(every)
        poll_pending(deps.supplier, client, request_id=rid, ticket_timeout=until)


def start_from_bot(client: Any, requester: str, text: str, channel: str) -> bool:
    """Entry point from the bot's /traveldesk handler for group requests."""
    return start(client, requester, text, channel, deps=bot_deps(), policy=bot_policy())
