"""In-memory Supplier for tests. Records every call; never touches the network."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

from traveldesk.ops.supplier.base import (
    BookingResult,
    CancelResult,
    Offer,
    PriceQuote,
    SearchQuery,
    SupplierError,
    SupplierUnsupported,
    TravelerDetails,
)


def make_offer(
    offer_id: str = "off_1",
    *,
    fare_paise: int = 1_042_800,
    route: str = "DEL-BOM",
    depart_at: datetime | None = None,
    arrive_at: datetime | None = None,
    refundable: bool | None = False,
) -> Offer:
    return Offer(
        supplier="fake",
        offer_id=offer_id,
        search_id="srch_1",
        route=route,
        depart_at=depart_at or datetime(2026, 12, 1, 0, 35, tzinfo=UTC),  # 06:05 IST
        arrive_at=arrive_at or datetime(2026, 12, 1, 2, 55, tzinfo=UTC),
        fare_paise=fare_paise,
        carrier="6E",
        flight_no="6E 2001",
        refundable=refundable,
    )


@dataclass
class FakeSupplier:
    name: str = "fake"
    supports_hold: bool = False
    offers: list[Offer] = field(default_factory=list)
    # What book() returns: "ticketed" | "held" | "pending" | "awaiting_payment" | "failed",
    # or an exception to raise.
    book_state: str | Exception = "ticketed"
    quotes: dict[str, PriceQuote] = field(default_factory=dict)  # offer_id → quote
    bookings: dict[str, BookingResult] = field(default_factory=dict)  # supplier_ref → state
    calls: list[tuple[str, str]] = field(default_factory=list)  # (op, trace_id)
    # Each offer id can be booked once (a second booking with another key raises), like a
    # supplier whose offers are single-use.
    single_use: bool = False
    pay_expires_at: datetime | None = None  # for book_state="awaiting_payment"
    _by_key: dict[str, BookingResult] = field(default_factory=dict)
    _used: set[str] = field(default_factory=set)
    _seq: int = 0

    def set_state(self, ref: str, state: str, **kw) -> None:
        """Move a booking on at the 'supplier' (what get_booking returns next)."""
        self.bookings[ref] = replace(self.bookings[ref], state=state, **kw)

    def search(self, q: SearchQuery, *, trace_id: str) -> list[Offer]:
        self.calls.append(("search", trace_id))
        return list(self.offers)

    def price_check(self, offer: Offer, *, trace_id: str, exact: bool = False) -> PriceQuote:
        """quotes[offer_id] (default: same fare/times). Returns a fresh offer (new id) each call;
        with exact=True a quote at another departure time counts as unavailable."""
        self.calls.append(("price_check", trace_id))
        q = self.quotes.get(offer.offer_id) or PriceQuote(
            True, offer.fare_paise, offer.depart_at, offer.arrive_at
        )
        if not q.available:
            return q
        if exact and q.depart_at and offer.depart_at and q.depart_at != offer.depart_at:
            return PriceQuote(False, None)
        if q.offer is not None:
            return q
        self._seq += 1
        fresh = replace(
            offer,
            offer_id=f"{offer.offer_id}~{self._seq}",
            fare_paise=q.fare_paise,
            depart_at=q.depart_at or offer.depart_at,
            arrive_at=q.arrive_at or offer.arrive_at,
        )
        return replace(q, offer=fresh)

    def hold(
        self, offer: Offer, traveler: TravelerDetails, *, idempotency_key: str, trace_id: str
    ) -> BookingResult:
        self.calls.append(("hold", trace_id))
        if not self.supports_hold:
            raise SupplierUnsupported("fake has no hold")
        return self._issue(offer, "held", idempotency_key)

    def book(
        self, offer: Offer, traveler: TravelerDetails, *, idempotency_key: str, trace_id: str
    ) -> BookingResult:
        self.calls.append(("book", trace_id))
        if isinstance(self.book_state, Exception):
            raise self.book_state
        if self.book_state == "failed":
            return BookingResult(state="failed", supplier_ref=None, message="fake refused")
        if self.single_use and offer.offer_id in self._used and idempotency_key not in self._by_key:
            raise SupplierError("offer already used", code="offer_used")
        self._used.add(offer.offer_id)
        return self._issue(offer, self.book_state, idempotency_key)

    def _issue(self, offer: Offer, state: str, key: str) -> BookingResult:
        if key in self._by_key:  # idempotency, like a real supplier
            return self._by_key[key]
        ref = f"FAKE{len(self._by_key) + 1:04d}"
        res = BookingResult(
            state=state,
            supplier_ref=ref,
            pnr=f"PNR{len(self._by_key) + 1:03d}" if state == "ticketed" else None,
            fare_paise=offer.fare_paise,
            depart_at=offer.depart_at,
            arrive_at=offer.arrive_at,
            pay_url=f"https://pay.example/{ref}" if state == "awaiting_payment" else None,
            pay_expires_at=self.pay_expires_at if state == "awaiting_payment" else None,
        )
        self._by_key[key] = res
        self.bookings[ref] = res
        return res

    def requote_hold(self, supplier_ref: str, offer: Offer, *, trace_id: str) -> BookingResult:
        self.calls.append(("requote_hold", trace_id))
        if not self.supports_hold:
            raise SupplierUnsupported("fake has no hold")
        old = self.bookings.get(supplier_ref) or BookingResult("held", supplier_ref)
        new = replace(old, state="held", fare_paise=offer.fare_paise)
        self.bookings[supplier_ref] = new
        return new

    def get_booking(self, supplier_ref: str, *, trace_id: str) -> BookingResult:
        self.calls.append(("get_booking", trace_id))
        if supplier_ref not in self.bookings:
            raise SupplierError("unknown booking", code="not_found")
        return self.bookings[supplier_ref]

    def cancel(self, supplier_ref: str, *, trace_id: str) -> CancelResult:
        self.calls.append(("cancel", trace_id))
        res = self.bookings[supplier_ref]
        self.bookings[supplier_ref] = replace(res, state="cancelled")
        return CancelResult(cancelled=True, refund_paise=res.fare_paise)


@dataclass
class FakeNotifier:
    """Records messages. `fail_targets` makes posts to those channels/users raise."""

    posts: list[tuple[str, str, list]] = field(default_factory=list)  # (channel, text, blocks)
    dms: list[tuple[str, str, list]] = field(default_factory=list)  # (user, text, blocks)
    fail_targets: set[str] = field(default_factory=set)

    def post(self, channel, text, blocks=None):
        if channel in self.fail_targets:
            raise RuntimeError("slack down")
        self.posts.append((channel, text, blocks))
        return f"ts{len(self.posts)}"

    def dm(self, user_id, text, blocks=None):
        if user_id in self.fail_targets:
            raise RuntimeError("slack down")
        self.dms.append((user_id, text, blocks))
        return f"dm{len(self.dms)}"
