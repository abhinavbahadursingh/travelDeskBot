"""Write path for the ops tables. Every booking goes requests → bookings → ledger debit.

Debit lifecycle (one row per supplier booking, key `debit:{supplier}:{supplier_ref}`):
  supplier accepts (charged)   → booking `pending`,          debit `pending`
  supplier gives a pay link    → booking `awaiting_payment`, no debit (nothing charged)
  ticket issued                → booking `ticketed`,         debit `confirmed` (created if missing)
  ends before ticketing        → booking `failed`/`cancelled`, debit `void` (if there was one)

All functions take an open Session and never commit; the caller's
`session_scope()` makes the whole chain one transaction, so a booking can't
land without its debit (or vice versa). Every write is idempotent.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from traveldesk.ops import gst
from traveldesk.ops.models import (
    BOOKING_PROGRESS,
    Booking,
    BookingStatus,
    Event,
    ItcStatus,
    LedgerEntry,
    LedgerStatus,
    LedgerType,
    Request,
    RequestStatus,
    Traveler,
)
from traveldesk.ops.supplier.base import BookingResult, Offer, offer_to_json
from traveldesk.ops.units import to_ist, utcnow


def mask_passport(number: str | None) -> str | None:
    if not number:
        return None
    s = str(number).strip()
    return "••••" + s[-4:] if len(s) > 4 else "••••"


def upsert_traveler(
    s: Session,
    *,
    slack_id: str,
    name: str,
    email: str | None = None,
    phone: str | None = None,
    passport_number: str | None = None,
    passport_exp: date | None = None,
    meal_pref: str | None = None,
    seat_pref: str | None = None,
) -> Traveler:
    """Create or refresh a traveller. The passport number is stored masked (last 4) only."""
    t = s.scalar(select(Traveler).where(Traveler.slack_id == slack_id))
    if t is None:
        t = Traveler(slack_id=slack_id, name=name)
        s.add(t)
    t.name = name
    for attr, value in (
        ("email", email),
        ("phone", phone),
        ("passport_no", mask_passport(passport_number)),
        ("passport_exp", passport_exp),
        ("meal_pref", meal_pref),
        ("seat_pref", seat_pref),
    ):
        if value is not None:  # never wipe a known value with "unknown"
            setattr(t, attr, value)
    s.flush()
    return t


def create_request(
    s: Session,
    *,
    slack_user: str,
    raw_text: str,
    parsed: dict[str, Any] | None = None,
    trace_id: str | None = None,
    legacy_booking_id: int | None = None,
    created_at: datetime | None = None,
) -> Request:
    """New request, or the existing mirror of the same legacy booking row."""
    if legacy_booking_id is not None:
        existing = s.scalar(select(Request).where(Request.legacy_booking_id == legacy_booking_id))
        if existing is not None:
            return existing
    r = Request(
        slack_user=slack_user,
        raw_text=raw_text,
        parsed_json=parsed,
        trace_id=trace_id,
        legacy_booking_id=legacy_booking_id,
        created_at=created_at or utcnow(),
    )
    s.add(r)
    s.flush()
    return r


def add_ledger(
    s: Session,
    booking: Booking,
    type_: LedgerType,
    amount_paise: int,
    *,
    idempotency_key: str,
    invoice_no: str | None = None,
    gst_no: str | None = None,
    status: LedgerStatus = LedgerStatus.CONFIRMED,
) -> LedgerEntry:
    """Insert a ledger row, or return the existing one with the same idempotency key (unchanged)."""
    if amount_paise < 0:
        raise ValueError("ledger amounts are positive; the type gives the direction")
    existing = s.scalar(select(LedgerEntry).where(LedgerEntry.idempotency_key == idempotency_key))
    if existing is not None:
        return existing
    e = LedgerEntry(
        booking_id=booking.id,
        type=type_,
        amount_paise=amount_paise,
        invoice_no=invoice_no,
        gst_no=gst_no,
        idempotency_key=idempotency_key,
        status=status,
    )
    s.add(e)
    s.flush()
    return e


def debit_key(booking: Booking) -> str:
    return f"debit:{booking.supplier}:{booking.supplier_ref}"


def booking_by_ref(s: Session, supplier: str, supplier_ref: str) -> Booking | None:
    return s.scalar(select(Booking).where(Booking.supplier == supplier, Booking.supplier_ref == supplier_ref))


def debit_of(s: Session, booking: Booking) -> LedgerEntry | None:
    return s.scalar(select(LedgerEntry).where(LedgerEntry.idempotency_key == debit_key(booking)))


RECORDABLE = ("awaiting_payment", "pending", "held", "ticketed")


def record_booking(
    s: Session,
    request: Request,
    *,
    supplier: str,
    result: BookingResult,
    offer: Offer | None = None,
    traveler: Traveler | None = None,
    route: str | None = None,
    depart_at: datetime | None = None,
    arrive_at: datetime | None = None,
    fare_paise: int | None = None,
    invoice_no: str | None = None,
    gst_no: str | None = None,
) -> Booking:
    """Store a supplier booking and keep its debit in step (see the module docstring).

    Idempotent on (supplier, supplier_ref): calling again updates the same row and never adds a
    second debit — whether the call comes from book_one, the group poller (again after a restart)
    or the legacy mirror. The status only moves forward (awaiting_payment → pending → held →
    ticketed); failed/cancelled/refunds are never reopened. Fare/route/times come from the result,
    then the offer, then the kwargs.
    """
    if result.state not in RECORDABLE:
        raise ValueError(f"only {'/'.join(RECORDABLE)} bookings are recorded, got {result.state!r}")
    if not result.supplier_ref:
        raise ValueError("supplier booking has no reference")

    fare = result.fare_paise if result.fare_paise is not None else fare_paise
    if fare is None and offer is not None:
        fare = offer.fare_paise
    new_status = BookingStatus(result.state)

    b = booking_by_ref(s, supplier, result.supplier_ref)
    if b is None:
        if fare is None:
            raise ValueError("no fare for the booking (result, kwarg and offer all empty)")
        b = Booking(
            request_id=request.id,
            supplier=supplier,
            supplier_ref=result.supplier_ref,
            route=route or (offer.route if offer else "?"),
            fare_paise=fare,
            status=new_status,
        )
        s.add(b)
    b.traveler_id = traveler.id if traveler else b.traveler_id
    b.pnr = result.pnr or b.pnr
    b.depart_at = result.depart_at or depart_at or (offer.depart_at if offer else None) or b.depart_at
    b.arrive_at = result.arrive_at or arrive_at or (offer.arrive_at if offer else None) or b.arrive_at
    if offer is not None:
        b.offer_json = offer_to_json(offer)
        if offer.refundable is not None:
            b.refundable = offer.refundable
    if result.pay_url:
        b.pay_url, b.pay_expires_at = result.pay_url, result.pay_expires_at or b.pay_expires_at
    old_fare = b.fare_paise
    if fare is not None:
        b.fare_paise = fare
    if b.status in BOOKING_PROGRESS and BOOKING_PROGRESS[new_status] >= BOOKING_PROGRESS[b.status]:
        b.status = new_status
    s.flush()

    if b.status is BookingStatus.PENDING:
        add_ledger(
            s, b, LedgerType.DEBIT, b.fare_paise, idempotency_key=debit_key(b), status=LedgerStatus.PENDING
        )
        add_event(s, b, "booking_pending", {"fare_paise": b.fare_paise}, dedupe_key=f"pending:{b.id}")
    elif b.status is BookingStatus.AWAITING_PAYMENT:
        add_event(
            s, b, "awaiting_payment",
            {"fare_paise": b.fare_paise, "pay_expires_at": b.pay_expires_at.isoformat() if b.pay_expires_at else None},
            dedupe_key=f"awaiting_payment:{b.id}",
        )  # fmt: skip
    elif b.status is BookingStatus.TICKETED:
        b.ticketed_at = b.ticketed_at or utcnow()
        _confirm_debit(s, b, old_fare=old_fare, invoice_no=invoice_no, gst_no=gst_no)
        if b.category is None:
            _estimate_gst(s, b)
        add_event(s, b, "ticketed", {"pnr": b.pnr, "fare_paise": b.fare_paise}, dedupe_key=f"ticketed:{b.id}")
    elif b.status is BookingStatus.HELD:
        add_event(s, b, "held", {"fare_paise": b.fare_paise}, dedupe_key=f"held:{b.id}:{b.fare_paise}")

    if b.status in (BookingStatus.HELD, BookingStatus.TICKETED):
        _refresh_request_status(s, request)
    return b


def _confirm_debit(
    s: Session, b: Booking, *, old_fare: int, invoice_no: str | None, gst_no: str | None
) -> None:
    """Ticket issued: the booking-time debit becomes confirmed (at the charged amount), or is created."""
    debit = debit_of(s, b)
    if debit is None:
        add_ledger(
            s,
            b,
            LedgerType.DEBIT,
            b.fare_paise,
            idempotency_key=debit_key(b),
            invoice_no=invoice_no,
            gst_no=gst_no,
        )
        return
    if debit.status is not LedgerStatus.CONFIRMED:
        if debit.status is LedgerStatus.VOID:
            add_event(s, b, "debit_unvoided", {"reason": "ticket issued after the booking was voided"},
                      dedupe_key=f"debit_unvoided:{b.id}")  # fmt: skip
        debit.status = LedgerStatus.CONFIRMED
    if debit.amount_paise != b.fare_paise:
        add_event(
            s, b, "debit_amount_changed", {"old": debit.amount_paise, "new": b.fare_paise, "booked_fare": old_fare},
            dedupe_key=f"debit_amount:{b.id}:{b.fare_paise}",
        )  # fmt: skip
        debit.amount_paise = b.fare_paise
    debit.invoice_no = invoice_no or debit.invoice_no
    debit.gst_no = gst_no or debit.gst_no
    s.flush()


def void_booking(
    s: Session, booking: Booking, *, status: BookingStatus = BookingStatus.FAILED, reason: str = ""
) -> bool:
    """The booking ended before a ticket: status failed/cancelled, its pending debit void.

    Only awaiting_payment/pending bookings can be voided (a ticketed one needs a refund instead).
    Returns True when something changed; calling it again is a no-op.
    """
    if status not in (BookingStatus.FAILED, BookingStatus.CANCELLED):
        raise ValueError("void_booking ends a booking as failed or cancelled")
    if booking.status not in (BookingStatus.AWAITING_PAYMENT, BookingStatus.PENDING):
        return False
    booking.status = status
    debit = debit_of(s, booking)
    if debit is not None and debit.status is LedgerStatus.PENDING:
        debit.status = LedgerStatus.VOID
    add_event(s, booking, f"booking_{status.value}", {"reason": reason}, dedupe_key=f"void:{booking.id}")
    s.flush()
    return True


def _refresh_request_status(s: Session, request: Request) -> None:
    if request.status in (RequestStatus.RECEIVED, RequestStatus.BOOKING):
        request.status = RequestStatus.BOOKED
    s.flush()


def add_event(
    s: Session,
    booking: Booking | None,
    kind: str,
    payload: dict[str, Any] | None = None,
    *,
    dedupe_key: str | None = None,
    created_at: datetime | None = None,
) -> Event | None:
    """Append an event. With a dedupe_key, a repeat is skipped and returns None."""
    if dedupe_key is not None and s.scalar(select(Event.id).where(Event.dedupe_key == dedupe_key)):
        return None
    ev = Event(
        booking_id=booking.id if booking else None,
        kind=kind,
        payload_json=payload,
        dedupe_key=dedupe_key,
        created_at=created_at or utcnow(),
    )
    try:
        with s.begin_nested():  # a concurrent writer may insert the same key between check and insert
            s.add(ev)
            s.flush()
    except IntegrityError:
        if dedupe_key is None:
            raise
        return None
    return ev


def attach_invoice(
    s: Session,
    booking: Booking,
    *,
    invoice_no: str,
    gst_no: str | None = None,
    invoice_amount_paise: int | None = None,
) -> LedgerEntry:
    """Put the supplier's invoice details on the booking's debit (the digest checks them)."""
    debit = s.scalar(select(LedgerEntry).where(LedgerEntry.idempotency_key == debit_key(booking)))
    if debit is None:
        raise LookupError(f"booking {booking.id} has no ledger debit")
    debit.invoice_no = invoice_no.strip()
    if gst_no is not None:
        debit.gst_no = gst_no.strip().upper() or None
    if invoice_amount_paise is not None:
        debit.invoice_amount_paise = invoice_amount_paise
    s.flush()
    return debit


def apply_gst(
    s: Session,
    b: Booking,
    *,
    company_gstin: str | None,
    company_state: str | None = None,
    category: str | None = None,
    taxable_value: Decimal | None = None,
    cabin: str | None = None,
    per_night_tariff: Decimal | None = None,
    supplier_gstin: str | None = None,
    supplier_state: str | None = None,
    invoice_no: str | None = None,
    invoice_date: date | None = None,
    invoice_url: str | None = None,
    invoice_gst: gst.InvoiceGst | None = None,
) -> gst.GstResult:
    """(Re)compute the booking's GST and store it. Unset arguments keep what the booking has.

    Category defaults to flight, the cabin to the booked offer's, the taxable value to the stored one
    or else the GST-inclusive fare backed out at the rate. The rate is the one in force on the
    booking date (IST). An ITC already marked claimed stays claimed.
    """
    category = category or b.category or "flight"
    booked_on = (to_ist(b.created_at) or to_ist(utcnow())).date()
    if cabin is None and b.offer_json:
        cabin = gst.cabin_of(b.offer_json.get("raw"))
    if taxable_value is None and b.taxable_value_paise is not None:
        taxable_value = gst.from_paise(b.taxable_value_paise)
    if taxable_value is None:
        rate = gst.pick_rate(category, booked_on, cabin=cabin, per_night_tariff=per_night_tariff).rate
        taxable_value = gst.taxable_from_inclusive(gst.from_paise(b.fare_paise), rate)
    if supplier_gstin is not None:
        b.supplier_gstin = gst.normalize(supplier_gstin) or None
    r = gst.compute(
        category,
        taxable_value,
        booked_on,
        cabin=cabin,
        per_night_tariff=per_night_tariff,
        supplier_gstin=b.supplier_gstin,
        supplier_state=supplier_state or b.supplier_state,
        company_gstin=company_gstin,
        company_state=company_state,
        invoice=invoice_gst,
    )
    b.category = r.category
    b.taxable_value_paise = gst.to_paise(r.taxable_value)
    b.gst_rate = r.gst_rate
    b.cgst_paise, b.sgst_paise, b.igst_paise = (gst.to_paise(x) for x in (r.cgst, r.sgst, r.igst))
    b.total_gst_paise = gst.to_paise(r.total_gst)
    b.computed_gst_paise = gst.to_paise(r.computed_gst)
    if invoice_gst is not None or b.gst_mismatch is None:
        b.gst_mismatch = r.mismatch
    b.supplier_state = r.supplier_state
    b.itc_eligible = r.itc_eligible
    if b.itc_status is not ItcStatus.CLAIMED:
        b.itc_status = ItcStatus(r.itc_status)
    b.invoice_no = (invoice_no or "").strip() or b.invoice_no
    b.invoice_date = invoice_date or b.invoice_date
    b.invoice_url = (invoice_url or "").strip() or b.invoice_url
    s.flush()
    return r


def _estimate_gst(s: Session, b: Booking) -> None:
    """At ticketing: a first GST estimate from the fare; the supplier invoice replaces it later."""
    from traveldesk.ops.settings import get_settings  # records stays settings-free otherwise

    cfg = get_settings()
    try:  # apply_gst only raises before it writes anything
        apply_gst(s, b, company_gstin=cfg.company_gstin, company_state=cfg.company_state_code)
    except (ValueError, KeyError) as e:  # no rate in force on that date, bad rates file…
        add_event(s, b, "gst_not_computed", {"error": str(e)}, dedupe_key=f"gst_not_computed:{b.id}")
