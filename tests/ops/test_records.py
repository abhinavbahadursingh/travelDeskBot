"""requests → bookings → ledger debit, idempotency and atomicity."""

import pytest
from sqlalchemy import func, select

from tests.ops.fakes import FakeSupplier, make_offer
from traveldesk.ops.flow import book_one
from traveldesk.ops.models import (
    Booking,
    BookingStatus,
    Event,
    LedgerEntry,
    LedgerType,
    Request,
    RequestStatus,
)
from traveldesk.ops.records import add_event, create_request, record_booking, upsert_traveler
from traveldesk.ops.supplier.base import BookingResult, SupplierError, TravelerDetails

DETAILS = TravelerDetails("U1", "Asha", "Rao", "asha@example.com", "+919800000000")


def _count(s, model, *where):
    return s.scalar(select(func.count()).select_from(model).where(*where))


def _setup(ops_session):
    with ops_session() as s:
        t = upsert_traveler(s, slack_id="U1", name="Asha Rao", email="asha@example.com")
        r = create_request(s, slack_user="U1", raw_text="DEL to BOM 1 Dec morning", parsed={"origin": "DEL"})
        return r.id, t.id


def test_book_one_writes_request_booking_and_debit(ops_session):
    req_id, trav_id = _setup(ops_session)
    sup = FakeSupplier()
    offer = make_offer(fare_paise=1_042_800)

    out = book_one(sup, request_id=req_id, traveler_id=trav_id, offer=offer, details=DETAILS, trace_id="t-1")

    assert out.state == "ticketed" and out.booking_id
    assert sup.calls == [("book", "t-1")]
    with ops_session() as s:
        b = s.get(Booking, out.booking_id)
        assert (b.status, b.pnr, b.fare_paise, b.route) == (
            BookingStatus.TICKETED,
            "PNR001",
            1_042_800,
            "DEL-BOM",
        )
        assert b.ticketed_at is not None and b.traveler_id == trav_id
        debits = s.scalars(select(LedgerEntry).where(LedgerEntry.booking_id == b.id)).all()
        assert [(e.type, e.amount_paise) for e in debits] == [(LedgerType.DEBIT, 1_042_800)]
        assert s.get(Request, req_id).status is RequestStatus.BOOKED


def test_book_one_retry_is_idempotent(ops_session):
    """A crash-and-retry with the same request/traveller/offer: one booking, one debit."""
    req_id, trav_id = _setup(ops_session)
    sup = FakeSupplier()
    offer = make_offer()
    a = book_one(sup, request_id=req_id, traveler_id=trav_id, offer=offer, details=DETAILS)
    b = book_one(sup, request_id=req_id, traveler_id=trav_id, offer=offer, details=DETAILS)
    assert a.booking_id == b.booking_id
    with ops_session() as s:
        assert _count(s, Booking) == 1
        assert _count(s, LedgerEntry) == 1
        assert _count(s, Event, Event.kind == "ticketed") == 1


def test_held_has_no_debit_until_ticketed(ops_session):
    req_id, _ = _setup(ops_session)
    with ops_session() as s:
        req = s.get(Request, req_id)
        b = record_booking(
            s, req, supplier="fake", result=BookingResult("held", "H1", fare_paise=500_000), route="DEL-BOM"
        )
        assert b.status is BookingStatus.HELD
        assert _count(s, LedgerEntry) == 0
        record_booking(
            s, req, supplier="fake", result=BookingResult("ticketed", "H1", pnr="ABC123", fare_paise=480_000)
        )
        assert _count(s, Booking) == 1
        entries = s.scalars(select(LedgerEntry)).all()
        assert [(e.type, e.amount_paise) for e in entries] == [(LedgerType.DEBIT, 480_000)]


def test_cancelled_booking_is_not_resurrected(ops_session):
    req_id, _ = _setup(ops_session)
    with ops_session() as s:
        req = s.get(Request, req_id)
        b = record_booking(
            s, req, supplier="fake", result=BookingResult("held", "C1", fare_paise=1), route="X-Y"
        )
        b.status = BookingStatus.CANCELLED
        s.flush()
        record_booking(s, req, supplier="fake", result=BookingResult("ticketed", "C1", fare_paise=1))
        assert b.status is BookingStatus.CANCELLED
        assert _count(s, LedgerEntry) == 0


def test_failed_booking_records_nothing(ops_session):
    """A refusal or an error writes no booking and no debit (pending is covered in test_debit_lifecycle)."""
    req_id, trav_id = _setup(ops_session)
    out = book_one(
        FakeSupplier(book_state="failed"),
        request_id=req_id,
        traveler_id=trav_id,
        offer=make_offer("o-failed"),
        details=DETAILS,
    )
    assert out.state == "failed" and out.booking_id is None
    out = book_one(
        FakeSupplier(book_state=SupplierError("boom")),
        request_id=req_id,
        traveler_id=trav_id,
        offer=make_offer("o-err"),
        details=DETAILS,
    )
    assert out.state == "failed" and "boom" in out.error
    with ops_session() as s:
        assert _count(s, Booking) == 0
        assert _count(s, LedgerEntry) == 0
        kinds = sorted(s.scalars(select(Event.kind)).all())
        assert kinds == ["book_failed", "book_failed"]


def test_chain_is_atomic(ops_session):
    """Booking + debit roll back together if anything in the transaction fails."""
    req_id, _ = _setup(ops_session)
    with pytest.raises(RuntimeError), ops_session() as s:
        req = s.get(Request, req_id)
        record_booking(s, req, supplier="fake", result=BookingResult("ticketed", "A1", pnr="P", fare_paise=9))
        raise RuntimeError("crash after recording")
    with ops_session() as s:
        assert _count(s, Booking) == 0
        assert _count(s, LedgerEntry) == 0


def test_record_requires_ref_fare_and_bookable_state(ops_session):
    req_id, _ = _setup(ops_session)
    with ops_session() as s:
        req = s.get(Request, req_id)
        with pytest.raises(ValueError, match="only awaiting_payment/pending/held/ticketed"):
            record_booking(s, req, supplier="fake", result=BookingResult("failed", "P1", fare_paise=1))
        with pytest.raises(ValueError, match="reference"):
            record_booking(s, req, supplier="fake", result=BookingResult("ticketed", None, fare_paise=1))
        with pytest.raises(ValueError, match="no fare"):
            record_booking(s, req, supplier="fake", result=BookingResult("ticketed", "P2"))


def test_event_dedupe(ops_session):
    with ops_session() as s:
        assert add_event(s, None, "fare_drop", {"x": 1}, dedupe_key="k1") is not None
        assert add_event(s, None, "fare_drop", {"x": 1}, dedupe_key="k1") is None
        assert _count(s, Event) == 1


def test_traveler_passport_is_masked_and_not_wiped(ops_session):
    with ops_session() as s:
        t = upsert_traveler(s, slack_id="U9", name="Ravi", passport_number="Z1234567", email="r@example.com")
        assert t.passport_no == "••••4567"
        upsert_traveler(s, slack_id="U9", name="Ravi K")  # profile without passport/email
        assert (t.name, t.passport_no, t.email) == ("Ravi K", "••••4567", "r@example.com")
