"""TBO Air API supplier — stub until credentials arrive.

Same interface as LetsFGSupplier, so switching is a SUPPLIER=tbo env change
once these are implemented.

TODO(TBO) — what TBO unlocks that LetsFG cannot do today:
- hold(): TBO "Book" without "Ticket" creates a held PNR (time-limited). Phase 2's
  "held + fare dropped ≥ ₹500 → re-quote" only becomes real with this; on LetsFG no
  booking is ever in `held`.
- Re-quote a held PNR: TBO FareQuote/FareRule on the held itinerary, then release the
  old hold and re-book at the lower fare (needs the hold time limit from the Book response).
- cancel(): TBO SendChangeRequest (cancellation) + GetChangeRequestStatus → refund amount.
  Drives ops_bookings.status ticketed → refund_pending → refunded and a ledger `refund` row.
- Wallet / agency balance: TBO deducts from a prepaid agency balance. Need a balance check
  before book() and a low-balance alert; ledger `debit` should carry TBO's invoice number.
- Invoices + GST: TBO returns invoice number and the GSTIN the ticket was billed under; map
  them into ops_ledger.invoice_no / gst_no (Phase 3 leakage digest relies on these).
- Auth: TBO token from Authenticate (valid for the day) — cache it, refresh on 401.
- Timezones: TBO returns airport-local times; convert with each airport's tz, not IST.
"""

from __future__ import annotations

from traveldesk.ops.supplier.base import (
    BookingResult,
    CancelResult,
    Offer,
    PriceQuote,
    SearchQuery,
    TravelerDetails,
)

_MSG = "TBO Air API is not integrated yet (no credentials). See TODO(TBO) in traveldesk/ops/supplier/tbo.py."


class TBOSupplier:
    name = "tbo"
    supports_hold = True  # TBO supports held PNRs; flip only matters once implemented

    def search(self, q: SearchQuery, *, trace_id: str) -> list[Offer]:
        raise NotImplementedError(_MSG)

    def price_check(self, offer: Offer, *, trace_id: str, exact: bool = False) -> PriceQuote:
        raise NotImplementedError(_MSG)

    def hold(
        self, offer: Offer, traveler: TravelerDetails, *, idempotency_key: str, trace_id: str
    ) -> BookingResult:
        raise NotImplementedError(_MSG)

    def book(
        self, offer: Offer, traveler: TravelerDetails, *, idempotency_key: str, trace_id: str
    ) -> BookingResult:
        raise NotImplementedError(_MSG)

    def requote_hold(self, supplier_ref: str, offer: Offer, *, trace_id: str) -> BookingResult:
        # TODO(TBO): FareQuote on the new result index; if lower, release the old hold and Book
        # (no Ticket) at the new fare before the old hold's time limit; return the new held ref.
        raise NotImplementedError(_MSG)

    def get_booking(self, supplier_ref: str, *, trace_id: str) -> BookingResult:
        # TODO(TBO): GetBookingDetails → segments' DepTime/ArrTime (airport-local; convert per airport
        # tz) so the watchdog sees schedule changes directly instead of via re-search.
        raise NotImplementedError(_MSG)

    def cancel(self, supplier_ref: str, *, trace_id: str) -> CancelResult:
        raise NotImplementedError(_MSG)
