"""Ledger debit at booking time, awaiting payment, and the two new digest buckets."""

import json
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy import func, select

from tests.ops.fakes import FakeSupplier, make_offer
from tests.ops.test_group import ADMIN, NAMES, Picky, Slack, deps, policy, roster, start
from traveldesk.ops import group
from traveldesk.ops.digest import OPEN_ACTION, OPEN_PAY_ACTION, RESOLVE_ACTION, build_message, find_leaks
from traveldesk.ops.flow import book_one
from traveldesk.ops.legacy import mirror_legacy_row, track_legacy_row
from traveldesk.ops.models import Booking, BookingStatus, Event, LedgerEntry, LedgerStatus, RosterStatus
from traveldesk.ops.records import create_request, record_booking, upsert_traveler, void_booking
from traveldesk.ops.settings import OpsSettings
from traveldesk.ops.supplier.base import BookingResult, TravelerDetails
from traveldesk.ops.units import utcnow

DETAILS = TravelerDetails("U1", "Asha", "Rao", "asha@example.com", "+919800000000")


def setup(ops_session):
    with ops_session() as s:
        t = upsert_traveler(s, slack_id="U1", name="Asha Rao")
        r = create_request(s, slack_user="U1", raw_text="DEL BOM", legacy_booking_id=None)
        return r.id, t.id


def debits(ops_session):
    with ops_session() as s:
        return [
            (e.status, e.amount_paise, e.idempotency_key)
            for e in s.scalars(select(LedgerEntry).order_by(LedgerEntry.id))
        ]


def booking(ops_session, bid=None):
    with ops_session() as s:
        return s.get(Booking, bid) if bid else s.scalars(select(Booking)).one()


def ticket(ref, fare=1_042_800, pnr="PNR9"):
    return BookingResult("ticketed", ref, pnr=pnr, fare_paise=fare)


# --- 1. debit at booking time --------------------------------------------------------------


def test_pending_booking_writes_pending_debit_at_once(ops_session):
    rid, tid = setup(ops_session)
    out = book_one(FakeSupplier(book_state="pending"), request_id=rid, traveler_id=tid,
                   offer=make_offer(fare_paise=1_042_800), details=DETAILS)  # fmt: skip
    assert out.state == "pending" and out.booking_id
    b = booking(ops_session)
    assert (b.status, b.pnr) == (BookingStatus.PENDING, None)
    assert debits(ops_session) == [(LedgerStatus.PENDING, 1_042_800, "debit:fake:FAKE0001")]


def test_ticket_confirms_the_same_debit(ops_session):
    rid, tid = setup(ops_session)
    book_one(
        FakeSupplier(book_state="pending"),
        request_id=rid,
        traveler_id=tid,
        offer=make_offer(),
        details=DETAILS,
    )
    with ops_session() as s:
        from traveldesk.ops.models import Request

        record_booking(s, s.get(Request, rid), supplier="fake", result=ticket("FAKE0001"))
    assert debits(ops_session) == [(LedgerStatus.CONFIRMED, 1_042_800, "debit:fake:FAKE0001")]
    assert booking(ops_session).status is BookingStatus.TICKETED


def test_charged_amount_differs_updates_the_debit(ops_session):
    rid, _ = setup(ops_session)
    with ops_session() as s:
        from traveldesk.ops.models import Request

        req = s.get(Request, rid)
        record_booking(
            s,
            req,
            supplier="fake",
            result=BookingResult("pending", "R1", fare_paise=1_000_000),
            route="DEL-BOM",
        )
        record_booking(s, req, supplier="fake", result=ticket("R1", fare=1_012_000))
        kinds = [e.kind for e in s.scalars(select(Event))]
    assert debits(ops_session) == [(LedgerStatus.CONFIRMED, 1_012_000, "debit:fake:R1")]
    assert "debit_amount_changed" in kinds


def test_stale_pending_answer_never_moves_a_ticket_back(ops_session):
    rid, _ = setup(ops_session)
    with ops_session() as s:
        from traveldesk.ops.models import Request

        req = s.get(Request, rid)
        record_booking(s, req, supplier="fake", result=ticket("R1"), route="DEL-BOM")
        record_booking(s, req, supplier="fake", result=BookingResult("pending", "R1", fare_paise=1_042_800))
    assert booking(ops_session).status is BookingStatus.TICKETED
    assert debits(ops_session) == [(LedgerStatus.CONFIRMED, 1_042_800, "debit:fake:R1")]


def test_void_only_before_ticketing(ops_session):
    rid, _ = setup(ops_session)
    with ops_session() as s:
        from traveldesk.ops.models import Request

        req = s.get(Request, rid)
        p = record_booking(
            s, req, supplier="fake", result=BookingResult("pending", "P1", fare_paise=5), route="X-Y"
        )
        t = record_booking(s, req, supplier="fake", result=ticket("T1", fare=7), route="X-Y")
        assert void_booking(s, p, reason="refused") is True
        assert void_booking(s, p, reason="again") is False  # idempotent
        assert void_booking(s, t) is False  # ticketed: refund flow, not void
    assert debits(ops_session) == [
        (LedgerStatus.VOID, 5, "debit:fake:P1"),
        (LedgerStatus.CONFIRMED, 7, "debit:fake:T1"),
    ]


def _group_pending(ops_session, **sup_kw):
    sup = Picky(book_state=sup_kw.pop("book_state", "pending"), **sup_kw)
    d = deps(sup)
    slack = Slack(NAMES)
    rid = start(slack, d, text="DEL Dubai for <@UA>")
    group.confirm(slack, rid, ADMIN, deps=d, policy=policy())
    return sup, slack, rid, next(iter(sup.bookings))


def test_poller_restart_does_not_double_write(ops_session):
    sup, slack, rid, ref = _group_pending(ops_session)
    sup.set_state(ref, "ticketed", pnr="TKT1")
    assert group.poll_pending(sup, slack) == 1
    assert group.poll_pending(sup, slack) == 0  # a second poller / restart
    with ops_session() as s:  # and an independent re-record of the same ticket
        from traveldesk.ops.models import Request

        record_booking(s, s.get(Request, rid), supplier="fake", result=sup.bookings[ref])
    assert debits(ops_session) == [(LedgerStatus.CONFIRMED, 2_500_000, f"debit:fake:{ref}")]


def test_supplier_failure_before_ticket_voids_debit(ops_session):
    sup, slack, rid, ref = _group_pending(ops_session)
    sup.set_state(ref, "failed", message="airline refused")
    group.poll_pending(sup, slack)
    assert booking(ops_session).status is BookingStatus.FAILED
    assert debits(ops_session)[0][0] is LedgerStatus.VOID
    assert roster(ops_session, rid)["UA"] == (RosterStatus.NEEDS_ACTION, "airline refused")


def test_supplier_cancel_before_ticket_voids_debit(ops_session):
    sup, slack, rid, ref = _group_pending(ops_session)
    sup.set_state(ref, "cancelled")
    group.poll_pending(sup, slack)
    assert booking(ops_session).status is BookingStatus.CANCELLED
    assert debits(ops_session)[0][0] is LedgerStatus.VOID


def test_pending_past_timeout_keeps_debit_pending(ops_session):
    sup, slack, rid, _ = _group_pending(ops_session)
    group.poll_pending(sup, slack, now=utcnow() + timedelta(hours=3), ticket_timeout=timedelta(hours=2))
    status, reason = roster(ops_session, rid)["UA"]
    assert status is RosterStatus.NEEDS_ACTION and "charged but not ticketed after 2h" in reason
    assert debits(ops_session)[0][0] is LedgerStatus.PENDING  # money may be gone: the digest flags it


# --- B. awaiting payment --------------------------------------------------------------------

EXPIRES = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)


def test_pay_link_is_tracked_without_a_debit(ops_session):
    rid, tid = setup(ops_session)
    out = book_one(FakeSupplier(book_state="awaiting_payment", pay_expires_at=EXPIRES), request_id=rid,
                   traveler_id=tid, offer=make_offer(), details=DETAILS)  # fmt: skip
    assert out.state == "awaiting_payment"
    b = booking(ops_session)
    assert (b.status, b.pay_url, b.pay_expires_at) == (
        BookingStatus.AWAITING_PAYMENT,
        "https://pay.example/FAKE0001",
        EXPIRES,
    )
    assert debits(ops_session) == []


def test_paid_link_then_ticket_writes_one_confirmed_debit(ops_session):
    sup, slack, rid, ref = _group_pending(ops_session, book_state="awaiting_payment", pay_expires_at=EXPIRES)
    assert "payment link" in slack.card["blocks"][2]["text"]["text"]
    assert debits(ops_session) == []

    sup.set_state(ref, "pending", pay_url=None)  # paid: now charged
    assert group.poll_pending(sup, slack) == 1
    assert debits(ops_session) == [(LedgerStatus.PENDING, 2_500_000, f"debit:fake:{ref}")]

    sup.set_state(ref, "ticketed", pnr="TKT2")
    group.poll_pending(sup, slack)
    assert debits(ops_session) == [(LedgerStatus.CONFIRMED, 2_500_000, f"debit:fake:{ref}")]


def test_paid_and_ticketed_between_polls_writes_confirmed_debit(ops_session):
    sup, slack, rid, ref = _group_pending(ops_session, book_state="awaiting_payment", pay_expires_at=EXPIRES)
    sup.set_state(ref, "ticketed", pnr="TKT3", pay_url=None)
    group.poll_pending(sup, slack)
    assert debits(ops_session) == [(LedgerStatus.CONFIRMED, 2_500_000, f"debit:fake:{ref}")]
    assert roster(ops_session, rid)["UA"][0] is RosterStatus.BOOKED


def test_expired_pay_link_fails_booking_without_debit(ops_session):
    sup, slack, rid, ref = _group_pending(ops_session, book_state="awaiting_payment", pay_expires_at=EXPIRES)
    assert group.poll_pending(sup, slack, now=EXPIRES - timedelta(minutes=1)) == 0
    assert group.poll_pending(sup, slack, now=EXPIRES + timedelta(minutes=1)) == 1
    assert booking(ops_session).status is BookingStatus.FAILED
    assert debits(ops_session) == []
    assert roster(ops_session, rid)["UA"] == (
        RosterStatus.NEEDS_ACTION,
        "payment link expired — nothing was charged",
    )


def test_supplier_cancelled_pay_link_fails_booking_without_debit(ops_session):
    sup, slack, rid, ref = _group_pending(ops_session, book_state="awaiting_payment", pay_expires_at=EXPIRES)
    sup.set_state(ref, "failed", message="LetsFG closed the payment link")
    group.poll_pending(sup, slack)
    assert booking(ops_session).status is BookingStatus.FAILED
    assert debits(ops_session) == []


# --- single bookings from the original bot (legacy rows) ----------------------------------------

PICK = {"origin": "DEL", "destination": "BOM", "airports": ["DEL", "BOM"], "domestic": True,
        "depart_at": "2026-12-01T06:05", "price": 10428.0, "currency": "INR",
        "signature": [["6E", "", "DEL", "BOM", "2026-12-01T06:05"]]}  # fmt: skip


def legacy_row(**kw):
    row = {"id": 41, "created_at": "2026-11-20T10:00:00", "slack_user": "UEMP1", "request": "DEL to BOM",
           "status": "booking_in_progress", "booking_ref": "bk_41", "pnr": None, "price": 10428.0,
           "currency": "INR", "charged_amount": None, "details": json.dumps(PICK)}  # fmt: skip
    row.update(kw)
    return row


def test_single_booking_plus_mirror_leaves_exactly_one_debit(ops_session):
    assert track_legacy_row(legacy_row(), "pending", pick=PICK)
    assert debits(ops_session) == [(LedgerStatus.PENDING, 1_042_800, "debit:letsfg:bk_41")]
    done = legacy_row(status="completed", pnr="ABC123", charged_amount=10500.0,
                      details=json.dumps({"status": "completed", "pnr": "ABC123", "charged_amount": 10500.0}))  # fmt: skip
    mirror_legacy_row(done, pick=PICK)
    mirror_legacy_row(done, pick=PICK)  # the mirror running twice
    assert debits(ops_session) == [(LedgerStatus.CONFIRMED, 1_050_000, "debit:letsfg:bk_41")]
    with ops_session() as s:
        assert s.scalar(select(func.count()).select_from(Booking)) == 1


def test_single_booking_failure_voids_debit(ops_session):
    track_legacy_row(legacy_row(), "pending", pick=PICK)
    track_legacy_row(legacy_row(status="failed"), "failed", reason="failed")
    assert booking(ops_session).status is BookingStatus.FAILED
    assert debits(ops_session)[0][0] is LedgerStatus.VOID


def test_single_booking_pay_link_lifecycle(ops_session):
    row = legacy_row(status="awaiting_payment", pay_url="https://letsfg.co/pay/x",
                     pay_expires_at="2030-01-01T12:00:00+00:00")  # fmt: skip
    track_legacy_row(row, "awaiting_payment", pick=PICK)
    b = booking(ops_session)
    assert (b.status, b.pay_url, b.pay_expires_at) == (
        BookingStatus.AWAITING_PAYMENT,
        "https://letsfg.co/pay/x",
        EXPIRES,
    )
    assert debits(ops_session) == []
    track_legacy_row(row, "pending", pick=PICK)  # paid
    assert debits(ops_session) == [(LedgerStatus.PENDING, 1_042_800, "debit:letsfg:bk_41")]


def test_single_booking_expired_pay_link_has_no_debit(ops_session):
    row = legacy_row(status="awaiting_payment", pay_url="https://letsfg.co/pay/x")
    track_legacy_row(row, "awaiting_payment", pick=PICK)
    track_legacy_row(row, "failed", reason="pay link expired")
    assert booking(ops_session).status is BookingStatus.FAILED
    assert debits(ops_session) == []


def test_bot_tracks_accepted_booking(monkeypatch):
    from traveldesk.slack import app
    from traveldesk.storage import db as legacy_db

    seen = []
    monkeypatch.setattr(
        app, "_track_in_ops", lambda row_id, state, pick=None, reason="": seen.append((row_id, state))
    )
    monkeypatch.setattr(app, "watch_booking", lambda *a, **k: None)
    row_id, _ = legacy_db.new_request("UEMP1", "DEL to BOM", "claude", thread_ts="t1")
    app._handle_book_result(
        Slack(), row_id, "UEMP1", None, PICK, {"status": "booking_in_progress", "booking_ref": "bk_1"}
    )
    row_id2, _ = legacy_db.new_request("UEMP1", "DEL to BOM 2", "claude", thread_ts="t2")
    app._handle_book_result(Slack(), row_id2, "UEMP1", None, PICK,
                            {"status": "awaiting_approval", "booking_ref": "bk_2", "approve_url": "https://p"})  # fmt: skip
    app._handle_book_result(Slack(), row_id2, "UEMP1", None, PICK, {"status": "failed", "message": "no"})
    assert seen == [(row_id, "pending"), (row_id2, "awaiting_payment")]


def test_bot_tracking_failure_never_breaks_booking(monkeypatch, caplog):
    from traveldesk.ops import legacy
    from traveldesk.slack import app

    def boom(*a, **k):
        raise RuntimeError("ops db locked")

    monkeypatch.setattr(legacy, "track_legacy_id", boom)
    app._track_in_ops(9, "pending", PICK)
    assert "ops tracking (pending) failed for booking 9" in caplog.text


# --- LetsFG mapping -------------------------------------------------------------------------------


def test_letsfg_pay_link_maps_to_awaiting_payment():
    from traveldesk.ops.supplier.letsfg import _result_from_rest

    r = _result_from_rest({"status": "payment_link", "booking_ref": "bk", "pay_url": "https://p",
                           "pay_valid_until": EXPIRES.timestamp()}, ref=None)  # fmt: skip
    assert (r.state, r.pay_url, r.pay_expires_at) == ("awaiting_payment", "https://p", EXPIRES)
    assert (
        _result_from_rest({"status": "booking_in_progress", "approve_url": "https://p"}, ref="bk").state
        == "pending"
    )
    assert _result_from_rest({"status": "booking_in_progress"}, ref="bk").pay_url is None
    assert _result_from_rest({"status": "expired"}, ref="bk").state == "failed"


# --- digest buckets ------------------------------------------------------------------------------

NOW = utcnow() + timedelta(days=1)


def cfg(**kw):
    return OpsSettings(**{"ops_slack_channel": "#ops", "dashboard_url": "http://mini:5173", **kw})


def _age_debit(ops_session, hours):
    with ops_session() as s:
        for e in s.scalars(select(LedgerEntry)):
            e.created_at = NOW - timedelta(hours=hours)


def _age_status(ops_session, minutes):
    with ops_session() as s:
        for b in s.scalars(select(Booking)):
            b.status_changed_at = NOW - timedelta(minutes=minutes)


def test_charged_no_ticket_bucket(ops_session):
    track_legacy_row(legacy_row(), "pending", pick=PICK)
    _age_debit(ops_session, 1)
    assert find_leaks(NOW, cfg())["charged_no_ticket"] == []  # under TICKET_TIMEOUT
    _age_debit(ops_session, 3)
    [item] = find_leaks(NOW, cfg())["charged_no_ticket"]
    assert "charged ₹10,428 3h ago, no ticket (ref bk_41)" in item.summary
    assert find_leaks(NOW, cfg(ticket_timeout="4h"))["charged_no_ticket"] == []

    _, blocks = build_message(find_leaks(NOW, cfg()), NOW, cfg())
    assert blocks[1]["text"]["text"] == "*1. Charged, no ticket* (1)"
    [actions] = [b for b in blocks if b["type"] == "actions"]
    assert [e["action_id"] for e in actions["elements"]] == [RESOLVE_ACTION, OPEN_ACTION]
    assert actions["elements"][1]["url"] == "http://mini:5173/?booking=41"


def test_charged_no_ticket_clears_once_ticketed_or_void(ops_session):
    track_legacy_row(legacy_row(), "pending", pick=PICK)
    _age_debit(ops_session, 3)
    track_legacy_row(legacy_row(), "failed")
    assert find_leaks(NOW, cfg())["charged_no_ticket"] == []


def test_awaiting_payment_bucket(ops_session):
    row = legacy_row(status="awaiting_payment", pay_url="https://letsfg.co/pay/x",
                     pay_expires_at="2030-01-01T12:00:00+00:00")  # fmt: skip
    track_legacy_row(row, "awaiting_payment", pick=PICK)
    _age_status(ops_session, 20)
    assert find_leaks(NOW, cfg())["awaiting_payment"] == []  # under 30 min
    _age_status(ops_session, 45)
    [item] = find_leaks(NOW, cfg())["awaiting_payment"]
    assert "pay link unpaid for 45m — link valid until 01 Jan 17:30 IST" in item.summary
    _, blocks = build_message(find_leaks(NOW, cfg()), NOW, cfg())
    [actions] = [b for b in blocks if b["type"] == "actions"]
    pay, resolve = actions["elements"]
    assert (pay["action_id"], pay["url"], pay["text"]["text"]) == (
        OPEN_PAY_ACTION,
        "https://letsfg.co/pay/x",
        "Open pay link",
    )
    assert resolve["action_id"] == RESOLVE_ACTION


def test_new_buckets_come_first_in_order(ops_session):
    track_legacy_row(legacy_row(), "pending", pick=PICK)  # charged, no ticket
    track_legacy_row(
        legacy_row(id=42, booking_ref="bk_42", pay_url="https://p"), "awaiting_payment", pick=PICK
    )
    rid, _ = setup(ops_session)
    with ops_session() as s:  # a ticketed booking with no invoice → unbilled
        from traveldesk.ops.models import Request

        record_booking(s, s.get(Request, rid), supplier="fake", result=ticket("T9"), route="DEL-BOM")
    _age_debit(ops_session, 3)
    _age_status(ops_session, 60)
    _, blocks = build_message(find_leaks(NOW, cfg()), NOW, cfg())
    titles = [
        b["text"]["text"] for b in blocks if b["type"] == "section" and b["text"]["text"].startswith("*")
    ]
    assert titles == [
        "*1. Charged, no ticket* (1)",
        "*2. Awaiting payment* (1)",
        "*3. Ticketed, no ledger debit or invoice* (1)",
    ]


# --- migration ----------------------------------------------------------------------------------


def test_new_states_accepted_by_db(ops_session, db_url):
    eng = sa.create_engine(db_url)
    with eng.begin() as c:
        c.execute(
            sa.text(
                "INSERT INTO ops_requests (slack_user, raw_text, created_at, status) VALUES ('U', 'x', '2026-01-01', 'received')"
            )
        )
        for st in ("awaiting_payment", "pending", "failed"):
            c.execute(sa.text(
                "INSERT INTO ops_bookings (request_id, supplier, supplier_ref, route, fare_paise, status, created_at, updated_at)"
                f" VALUES (1, 'f', '{st}', 'X', 1, '{st}', '2026-01-01', '2026-01-01')"
            ))  # fmt: skip
        c.execute(
            sa.text(
                "INSERT INTO ops_roster (request_id, slack_id, status, attempt, updated_at) VALUES (1, 'U', 'price_changed', 0, '2026-01-01')"
            )
        )
        c.execute(
            sa.text(
                "INSERT INTO ops_ledger (booking_id, type, amount_paise, created_at) VALUES (1, 'debit', 1, '2026-01-01')"
            )
        )
        assert c.execute(sa.text("SELECT status FROM ops_ledger")).scalar() == "confirmed"  # default
    eng.dispose()
