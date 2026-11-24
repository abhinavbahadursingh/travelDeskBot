"""LetsFG supplier: adapts the existing REST client (traveldesk.clients.letsfg_rest) to `Supplier`.

What LetsFG's REST API has: search, book, get_booking. So:
- price_check = re-search the same route/date and find the same flight by signature.
- hold / cancel = SupplierUnsupported (see TODO(TBO) in tbo.py).

LetsFG returns airport-local times with no offset. Times at Indian airports are
read as IST; times at other airports are left as None (no airport-tz table yet)
rather than stored wrong.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from traveldesk.ops.supplier.base import (
    BookingResult,
    CancelResult,
    Offer,
    PriceQuote,
    SearchQuery,
    SupplierError,
    SupplierUnsupported,
    TravelerDetails,
    logged_call,
)
from traveldesk.ops.units import IST, as_utc, to_paise

log = logging.getLogger("traveldesk.ops.supplier.letsfg")

_CABIN = {"economy": "M", "premium_economy": "W", "business": "C", "first": "F"}


def _rest():
    # Imported lazily: letsfg_rest imports traveldesk.config, which needs the bot's secrets.
    from traveldesk.clients import letsfg_rest

    return letsfg_rest


def _scope():
    from traveldesk.booking import flight_scope

    return flight_scope


def _segment_time(seg: dict, key: str, side: str) -> datetime | None:
    raw = str(seg.get(key) or "")[:19]
    if not raw:
        return None
    try:
        naive = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if naive.tzinfo is not None:
        return as_utc(naive)
    if _scope().airport_country(seg, side) == "IN":
        return as_utc(naive, naive_tz=IST)
    return None  # foreign airport, offset unknown


def offer_from_raw(raw: dict, search_id: str | None) -> Offer | None:
    """LetsFG offer dict → Offer. None when it has no id or no usable price."""
    scope = _scope()
    price = scope.offer_price(raw)
    offer_id = raw.get("id") or raw.get("offer_id")
    if price is None or not offer_id:
        return None
    segs = scope.segments(raw)
    ob = raw.get("outbound")
    out_segs = (
        [s for s in (ob.get("segments") or []) if isinstance(s, dict)] if isinstance(ob, dict) else segs
    )
    first, last_out = (out_segs[0], out_segs[-1]) if out_segs else ({}, {})
    route = "-".join(scope.airports(raw)) or "?"
    conditions = raw.get("conditions") if isinstance(raw.get("conditions"), dict) else {}
    refund_rule = str(conditions.get("refund_before_departure") or "").lower()
    refundable = None if not refund_rule else ("not" not in refund_rule)
    return Offer(
        supplier="letsfg",
        offer_id=str(offer_id),
        search_id=search_id,
        route=route,
        depart_at=_segment_time(first, "departure", "origin") if first else None,
        arrive_at=_segment_time(last_out, "arrival", "destination") if last_out else None,
        fare_paise=to_paise(price),
        carrier=first.get("airline"),
        flight_no=first.get("flight_no"),
        refundable=refundable,
        raw=raw,
    )


def _legs(raw: dict) -> tuple[list[dict], list[dict]]:
    ob, ib = raw.get("outbound"), raw.get("inbound")
    if isinstance(ob, dict):
        out = [s for s in ob.get("segments") or [] if isinstance(s, dict)]
        back = [s for s in (ib.get("segments") or [] if isinstance(ib, dict) else []) if isinstance(s, dict)]
        return out, back
    return _scope().segments(raw), []  # flat shape (e.g. rebuilt from a stored signature): one way


def _trip(raw: dict) -> tuple[str, str, str, str | None]:
    """(origin, destination, outbound date, return date) for a re-search, from the segments."""
    out, back = _legs(raw)
    if not out:
        raise SupplierError("offer has no segments to re-check", code="bad_offer")
    origin = str(out[0].get("origin") or "").upper()
    destination = str(out[-1].get("destination") or "").upper()
    date_from = str(out[0].get("departure") or "")[:10]
    return_from = str(back[0].get("departure") or "")[:10] if back else None
    if not (origin and destination and date_from):
        raise SupplierError("offer segments lack airports/dates", code="bad_offer")
    return origin, destination, date_from, return_from or None


def _same_flight(want: tuple, raw: dict) -> bool:
    """Same segments at the same times. A blank flight number on either side matches any."""
    got = _scope().signature(raw)
    if len(got) != len(want):
        return False
    return all(
        (w[0], w[2], w[3], w[4]) == (g[0], g[2], g[3], g[4]) and (not w[1] or not g[1] or w[1] == g[1])
        for w, g in zip(want, got, strict=True)
    )


def _same_flight_numbers(want: tuple, raw: dict) -> bool:
    """Same airline + flight number on every segment (times may differ). Needs flight numbers."""
    got = _scope().signature(raw)
    if len(got) != len(want) or not all(w[1] for w in want):
        return False
    return all((w[0], w[1], w[2], w[3]) == (g[0], g[1], g[2], g[3]) for w, g in zip(want, got, strict=True))


def raw_from_signature(signature: list) -> dict:
    """A minimal LetsFG-shaped offer from the bot's stored pick signature, enough to re-check it."""
    return {
        "segments": [
            {"airline": a, "flight_no": f, "origin": o, "destination": d, "departure": dep}
            for a, f, o, d, dep in (tuple(s) for s in signature or [] if len(s) == 5)
        ]
    }


def offer_from_pick(pick: dict, *, route: str, depart_at: datetime | None, fare_paise: int) -> Offer | None:
    """An Offer from the bot's pick record (search_id, offer_id, flight signature). None without a signature.

    Bookable (search_id + offer_id) and re-checkable (signature) — what the group flow and the
    legacy mirror store.
    """
    raw = raw_from_signature(pick.get("signature") or [])
    if not raw["segments"]:
        return None
    first = raw["segments"][0]
    return Offer(
        supplier="letsfg",
        offer_id=str(pick.get("offer_id") or ""),
        search_id=pick.get("search_id"),
        route=route,
        depart_at=depart_at,
        arrive_at=None,
        fare_paise=fare_paise,
        carrier=first["airline"] or None,
        flight_no=first["flight_no"] or None,
        raw=raw,
    )


_AWAITING_PAYMENT = ("payment_link", "awaiting_payment", "awaiting_approval")
_ENDED = ("need_info", "needs_attention", "not_started", "approval_expired", "expired", "failed")


def _epoch_utc(value) -> datetime | None:
    try:
        return datetime.fromtimestamp(float(value), UTC) if value not in (None, "") else None
    except (TypeError, ValueError, OverflowError):
        return None


def _result_from_rest(out: dict, *, ref: str | None) -> BookingResult:
    """letsfg_rest's status dict → BookingResult.

    payment_link / awaiting_payment = LetsFG gave a secure pay page and nothing is charged yet;
    paying it starts the booking under `booking_ref`. Everything still in progress = pending.
    """
    status = out.get("status")
    charged = out.get("charged_amount")
    fare = to_paise(charged) if charged not in (None, "") else None
    if status == "completed":
        state = "ticketed"
    elif status == "cancelled":
        state = "cancelled"
    elif status in _ENDED:
        state = "failed"
    elif status in _AWAITING_PAYMENT:
        state = "awaiting_payment"
    else:
        state = "pending"  # booking_in_progress, authorised, card_issued, awaiting_settlement…
    awaiting = state == "awaiting_payment"
    return BookingResult(
        state=state,
        supplier_ref=out.get("booking_ref") or ref,
        pnr=out.get("pnr"),
        fare_paise=fare,
        pay_url=(out.get("pay_url") or out.get("approve_url")) if awaiting else None,
        pay_expires_at=_epoch_utc(out.get("pay_valid_until")) if awaiting else None,
        message=str(out.get("message") or ""),
        raw={k: v for k, v in out.items() if k != "error_detail"},
    )


class LetsFGSupplier:
    name = "letsfg"
    supports_hold = False

    def search(self, q: SearchQuery, *, trace_id: str) -> list[Offer]:
        def call() -> list[Offer]:
            out = _rest().search_flights(
                origin=q.origin,
                destination=q.destination,
                date_from=q.depart_date.isoformat(),
                return_from=q.return_date.isoformat() if q.return_date else None,
                adults=q.adults,
                cabin_class=_CABIN.get(q.cabin, "M"),
            )
            if out.get("status") != "ok":
                raise SupplierError(str(out.get("message") or "search failed"), code=str(out.get("status")))
            sid = out.get("search_id")
            return [o for o in (offer_from_raw(r, sid) for r in out.get("offers") or []) if o]

        return logged_call(self.name, "search", trace_id, call, route=f"{q.origin}-{q.destination}")

    def price_check(self, offer: Offer, *, trace_id: str, exact: bool = False) -> PriceQuote:
        """Re-search the trip and find the same flight.

        1. Same flights at the same times → current price.
        2. Else the same flight numbers at other times → the schedule moved; the quote carries
           the new times (LetsFG's booking status has no times, so this is how drift shows up).
        3. Else → not available.
        """

        def call() -> PriceQuote:
            want = _scope().signature(offer.raw)
            if not want:
                raise SupplierError("offer has no segments to re-check", code="bad_offer")
            origin, destination, date_from, return_from = _trip(offer.raw)
            out = _rest().search_flights(
                origin=origin, destination=destination, date_from=date_from, return_from=return_from
            )
            if out.get("status") != "ok":
                raise SupplierError(
                    str(out.get("message") or "search failed"), code="search_failed", retryable=True
                )
            raws = out.get("offers") or []
            for matcher in (_same_flight,) if exact else (_same_flight, _same_flight_numbers):
                hits = [
                    o
                    for o in (offer_from_raw(r, out.get("search_id")) for r in raws if matcher(want, r))
                    if o
                ]
                if hits:
                    best = min(hits, key=lambda o: o.fare_paise)
                    return PriceQuote(True, best.fare_paise, best.depart_at, best.arrive_at, best)
            return PriceQuote(available=False, fare_paise=None)

        return logged_call(self.name, "price_check", trace_id, call, offer_id=offer.offer_id, exact=exact)

    def requote_hold(self, supplier_ref: str, offer: Offer, *, trace_id: str) -> BookingResult:
        def call() -> BookingResult:
            raise SupplierUnsupported("LetsFG has no held bookings to re-quote")

        return logged_call(self.name, "requote_hold", trace_id, call, ref=supplier_ref)

    def hold(
        self, offer: Offer, traveler: TravelerDetails, *, idempotency_key: str, trace_id: str
    ) -> BookingResult:
        def call() -> BookingResult:
            raise SupplierUnsupported("LetsFG has no hold API")

        return logged_call(self.name, "hold", trace_id, call)

    def book(
        self, offer: Offer, traveler: TravelerDetails, *, idempotency_key: str, trace_id: str
    ) -> BookingResult:
        if not offer.search_id:
            raise SupplierError("LetsFG needs the search_id the offer came from", code="bad_offer")
        passenger: dict[str, Any] = {
            "given_name": traveler.given_name,
            "family_name": traveler.family_name,
            "email": traveler.email,
            "phone": traveler.phone,
            **traveler.extra,
        }

        def call() -> BookingResult:
            out = _rest().book_flight(offer.search_id, offer.offer_id, passenger, idempotency_key)
            return _result_from_rest(out, ref=None)

        return logged_call(self.name, "book", trace_id, call, offer_id=offer.offer_id, key=idempotency_key)

    def get_booking(self, supplier_ref: str, *, trace_id: str) -> BookingResult:
        def call() -> BookingResult:
            out = _rest().get_booking(supplier_ref)
            if out.get("transient"):
                raise SupplierError(str(out.get("message")), code="transient", retryable=True)
            return _result_from_rest(out, ref=supplier_ref)

        return logged_call(self.name, "get_booking", trace_id, call, ref=supplier_ref)

    def cancel(self, supplier_ref: str, *, trace_id: str) -> CancelResult:
        def call() -> CancelResult:
            raise SupplierUnsupported("LetsFG has no flight-cancel API; cancel on letsfg.co")

        return logged_call(self.name, "cancel", trace_id, call, ref=supplier_ref)
