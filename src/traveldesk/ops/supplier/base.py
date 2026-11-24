"""Supplier protocol: the only door to an airline/consolidator API.

Nothing outside `traveldesk.ops.supplier` may call LetsFG/TBO directly. Every
call goes through `logged_call`, which writes one log line with the trace_id,
operation, duration and outcome.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Protocol, TypeVar, runtime_checkable

log = logging.getLogger("traveldesk.ops.supplier")

T = TypeVar("T")


class SupplierError(RuntimeError):
    """The supplier refused or failed. `retryable` = safe to try again later with the same key."""

    def __init__(self, message: str, *, code: str = "error", retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class SupplierUnsupported(NotImplementedError):
    """This supplier has no such operation (e.g. LetsFG has no hold or cancel API)."""


def new_trace_id() -> str:
    return uuid.uuid4().hex


def logged_call[T](supplier: str, op: str, trace_id: str, fn: Callable[[], T], **ctx: Any) -> T:
    """Run one supplier call and log it. Context values must not contain passenger PII."""
    start = time.monotonic()
    try:
        result = fn()
    except SupplierUnsupported:
        log.info("supplier=%s op=%s trace_id=%s outcome=unsupported %s", supplier, op, trace_id, ctx)
        raise
    except Exception as e:
        ms = int((time.monotonic() - start) * 1000)
        log.warning(
            "supplier=%s op=%s trace_id=%s outcome=error ms=%d error=%s %s",
            supplier,
            op,
            trace_id,
            ms,
            type(e).__name__,
            ctx,
        )
        raise
    ms = int((time.monotonic() - start) * 1000)
    log.info("supplier=%s op=%s trace_id=%s outcome=ok ms=%d %s", supplier, op, trace_id, ms, ctx)
    return result


# --- value types (supplier-neutral; money in paise, times aware UTC) --------


@dataclass(frozen=True)
class SearchQuery:
    origin: str
    destination: str
    depart_date: date
    return_date: date | None = None
    adults: int = 1
    cabin: str = "economy"


@dataclass(frozen=True)
class Offer:
    supplier: str
    offer_id: str
    search_id: str | None
    route: str  # "DEL-BOM"
    depart_at: datetime | None
    arrive_at: datetime | None
    fare_paise: int
    carrier: str | None = None
    flight_no: str | None = None
    refundable: bool | None = None
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)


def offer_to_json(o: Offer) -> dict[str, Any]:
    """Offer → JSON-safe dict, stored on the booking so the watchdog can re-check it later."""
    return {
        "supplier": o.supplier,
        "offer_id": o.offer_id,
        "search_id": o.search_id,
        "route": o.route,
        "depart_at": o.depart_at.isoformat() if o.depart_at else None,
        "arrive_at": o.arrive_at.isoformat() if o.arrive_at else None,
        "fare_paise": o.fare_paise,
        "carrier": o.carrier,
        "flight_no": o.flight_no,
        "refundable": o.refundable,
        "raw": o.raw,
    }


def offer_from_json(d: dict[str, Any]) -> Offer:
    def ts(v: Any) -> datetime | None:
        return datetime.fromisoformat(v) if v else None

    return Offer(
        supplier=d["supplier"],
        offer_id=d["offer_id"],
        search_id=d.get("search_id"),
        route=d["route"],
        depart_at=ts(d.get("depart_at")),
        arrive_at=ts(d.get("arrive_at")),
        fare_paise=int(d["fare_paise"]),
        carrier=d.get("carrier"),
        flight_no=d.get("flight_no"),
        refundable=d.get("refundable"),
        raw=d.get("raw") or {},
    )


@dataclass(frozen=True)
class PriceQuote:
    available: bool
    fare_paise: int | None
    depart_at: datetime | None = None
    arrive_at: datetime | None = None
    offer: Offer | None = None  # the bookable offer at this price, when the supplier returns one


@dataclass(frozen=True)
class TravelerDetails:
    """What a supplier needs to issue a ticket. Built from the profile right before booking."""

    slack_id: str
    given_name: str
    family_name: str
    email: str
    phone: str
    extra: dict[str, Any] = field(default_factory=dict, repr=False)  # DOB, passport, address… (PII)


@dataclass(frozen=True)
class BookingResult:
    """Outcome of book()/get_booking().

    state: ticketed | held | pending (accepted, ticket not issued yet) |
           awaiting_payment (pay link given, nothing charged) | failed | cancelled
    """

    state: str
    supplier_ref: str | None
    pnr: str | None = None
    fare_paise: int | None = None
    depart_at: datetime | None = None
    arrive_at: datetime | None = None
    pay_url: str | None = None
    pay_expires_at: datetime | None = None  # when the pay link stops working, if the supplier says
    message: str = ""
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)


@dataclass(frozen=True)
class CancelResult:
    cancelled: bool
    refund_paise: int | None = None
    message: str = ""


@runtime_checkable
class Supplier(Protocol):
    name: str
    supports_hold: bool

    def search(self, q: SearchQuery, *, trace_id: str) -> list[Offer]: ...

    def price_check(self, offer: Offer, *, trace_id: str, exact: bool = False) -> PriceQuote:
        """Current price for this flight, with a fresh bookable `offer` when the supplier has one.

        exact=True: only the same carrier, flight number(s) and departure time (for booking).
        exact=False: may also return the same flight numbers at a new time (the watchdog uses that
        to notice schedule changes).
        """
        ...

    def hold(
        self, offer: Offer, traveler: TravelerDetails, *, idempotency_key: str, trace_id: str
    ) -> BookingResult:
        """Reserve without ticketing. Raise SupplierUnsupported if `supports_hold` is False."""
        ...

    def book(
        self, offer: Offer, traveler: TravelerDetails, *, idempotency_key: str, trace_id: str
    ) -> BookingResult:
        """Book and ticket. Same idempotency_key twice must never create two bookings."""
        ...

    def requote_hold(self, supplier_ref: str, offer: Offer, *, trace_id: str) -> BookingResult:
        """Move a held booking to the fare in `offer` (same flight, lower price).

        Returns the held booking at its new fare. SupplierUnsupported where holds don't exist.
        """
        ...

    def get_booking(self, supplier_ref: str, *, trace_id: str) -> BookingResult:
        """Current state. depart_at/arrive_at are set when the supplier reports schedule times."""
        ...

    def cancel(self, supplier_ref: str, *, trace_id: str) -> CancelResult: ...
