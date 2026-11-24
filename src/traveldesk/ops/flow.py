"""Book one traveller through a Supplier and record it: requests → bookings → ledger debit.

The supplier call runs outside any DB transaction (no lock held during network
I/O); the result is then recorded in one transaction. The idempotency key is
stable per (request, traveller, offer), so a retry after a crash returns the
supplier's existing booking instead of buying a second ticket.
"""

from __future__ import annotations

from dataclasses import dataclass

from traveldesk.ops import db as ops_db
from traveldesk.ops.models import Request, Traveler
from traveldesk.ops.records import RECORDABLE, add_event, record_booking
from traveldesk.ops.supplier.base import (
    BookingResult,
    Offer,
    Supplier,
    SupplierError,
    TravelerDetails,
    new_trace_id,
)


@dataclass(frozen=True)
class BookOutcome:
    state: str  # ticketed | held | pending | awaiting_payment | failed
    booking_id: int | None
    result: BookingResult | None
    error: str = ""


def idempotency_key(request_id: int, traveler_id: int, attempt: int = 0) -> str:
    """Stable per (request, traveller, attempt) — not per offer: with fresh offers per traveller a
    crash-retry may hold a new offer id, and must still get the supplier's existing booking back.
    A user-initiated retry bumps `attempt`, so the supplier doesn't replay the earlier refusal."""
    return f"td-{request_id}-{traveler_id}" + (f"-a{attempt}" if attempt else "")


def book_one(
    supplier: Supplier,
    *,
    request_id: int,
    traveler_id: int,
    offer: Offer,
    details: TravelerDetails,
    trace_id: str | None = None,
    attempt: int = 0,
) -> BookOutcome:
    trace_id = trace_id or new_trace_id()
    key = idempotency_key(request_id, traveler_id, attempt)
    try:
        result = supplier.book(offer, details, idempotency_key=key, trace_id=trace_id)
    except SupplierError as e:
        _note(
            request_id, "book_failed", {"traveler_id": traveler_id, "error": str(e), "code": e.code}, trace_id
        )
        return BookOutcome("failed", None, None, str(e))

    # Accepted in any form → recorded now: pending gets its pending debit at once, awaiting_payment
    # (pay link, nothing charged) is tracked without one, ticketed/held as before.
    if result.state in RECORDABLE and result.supplier_ref:
        with ops_db.session_scope() as s:
            req = s.get(Request, request_id)
            trav = s.get(Traveler, traveler_id)
            if req is None or trav is None:
                raise LookupError(f"request {request_id} / traveller {traveler_id} not found")
            b = record_booking(s, req, supplier=supplier.name, result=result, offer=offer, traveler=trav)
            return BookOutcome(result.state, b.id, result)

    _note(
        request_id,
        "book_failed",
        {
            "traveler_id": traveler_id,
            "ref": result.supplier_ref,
            "msg": result.message,
            "state": result.state,
        },
        trace_id,
    )
    return BookOutcome(
        "failed" if result.state not in RECORDABLE else result.state, None, result, result.message
    )


def _note(request_id: int, kind: str, payload: dict, trace_id: str) -> None:
    with ops_db.session_scope() as s:
        add_event(s, None, kind, {"request_id": request_id, "trace_id": trace_id, **payload})
