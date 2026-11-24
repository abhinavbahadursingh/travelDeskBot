"""Phase 2: fare-drop and schedule-drift watchdog."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from tests.ops.fakes import FakeNotifier, FakeSupplier, make_offer
from traveldesk.ops.models import Booking, BookingStatus, Event, SegmentSnapshot
from traveldesk.ops.notify import ACK_ACTION
from traveldesk.ops.records import create_request, record_booking, upsert_traveler
from traveldesk.ops.settings import OpsSettings
from traveldesk.ops.supplier.base import BookingResult, PriceQuote, SupplierError
from traveldesk.ops.watchdog import run_watchdog

NOW = datetime(2026, 11, 25, 6, 0, tzinfo=UTC)
DEP = datetime(2026, 12, 1, 0, 35, tzinfo=UTC)  # 06:05 IST
ARR = datetime(2026, 12, 1, 2, 55, tzinfo=UTC)  # 08:25 IST


def cfg(**kw):
    base = dict(ops_slack_channel="#travel-ops", watchdog_min_recheck_minutes=150)
    return OpsSettings(**{**base, **kw})


def make_booking(ops_session, *, state="ticketed", fare=1_000_000, refundable=True, depart=DEP, ref="R1"):
    offer = make_offer(f"o-{ref}", fare_paise=fare, depart_at=depart, arrive_at=depart + (ARR - DEP),
                       refundable=refundable)  # fmt: skip
    with ops_session() as s:
        t = upsert_traveler(s, slack_id="UTRAV", name="Asha Rao")
        r = create_request(s, slack_user="UTRAV", raw_text="DEL BOM")
        b = record_booking(
            s, r, supplier="fake",
            result=BookingResult(state, ref, pnr="PNR1" if state == "ticketed" else None, fare_paise=fare),
            offer=offer, traveler=t,
        )  # fmt: skip
        return b.id, offer


def quote(offer, fare=None, dep=None, arr=None):
    return PriceQuote(True, fare if fare is not None else offer.fare_paise, dep or offer.depart_at,
                      arr or offer.arrive_at)  # fmt: skip


def events(ops_session, kind=None):
    with ops_session() as s:
        q = select(Event).order_by(Event.id)
        if kind:
            q = q.where(Event.kind == kind)
        return s.scalars(q).all()


def booking(ops_session, bid):
    with ops_session() as s:
        return s.get(Booking, bid)


# --- fare -------------------------------------------------------------------


def test_ticketed_refundable_drop_notifies_ops_only(ops_session):
    bid, offer = make_booking(ops_session, refundable=True)
    sup = FakeSupplier(quotes={offer.offer_id: quote(offer, fare=950_000)})
    n = FakeNotifier()

    rep = run_watchdog(sup, n, now=NOW, settings=cfg())

    assert (rep.checked, rep.errors, len(rep.alerts), rep.delivered) == (1, 0, 1, 1)
    [(channel, text, blocks)] = n.posts
    assert channel == "#travel-ops"
    assert text == "Optimization caught: fare dropped ₹500"
    button = blocks[-1]["elements"][0]
    assert button["action_id"] == ACK_ACTION and button["text"]["text"] == "Acknowledge"
    assert n.dms == []
    assert booking(ops_session, bid).fare_paise == 1_000_000  # notify only: nothing rebooked
    with ops_session() as s:
        snap = s.scalars(select(SegmentSnapshot)).one()
        assert (snap.fare_paise, snap.captured_at) == (950_000, NOW)


@pytest.mark.parametrize(
    "drop,refundable,alerts",
    [
        (49_999, True, 0),  # ₹499.99: under threshold
        (50_000, True, 1),  # exactly ₹500
        (200_000, False, 0),  # non-refundable ticket: no action
        (200_000, None, 0),  # refundability unknown: no action
    ],
)
def test_fare_threshold_and_refundability(ops_session, drop, refundable, alerts):
    _, offer = make_booking(ops_session, refundable=refundable)
    sup = FakeSupplier(quotes={offer.offer_id: quote(offer, fare=1_000_000 - drop)})
    rep = run_watchdog(sup, FakeNotifier(), now=NOW, settings=cfg())
    assert len(rep.alerts) == alerts


def test_held_drop_is_requoted(ops_session):
    bid, offer = make_booking(ops_session, state="held", fare=1_000_000)
    sup = FakeSupplier(supports_hold=True, quotes={offer.offer_id: quote(offer, fare=900_000)})
    n = FakeNotifier()

    run_watchdog(sup, n, now=NOW, settings=cfg())

    assert ("requote_hold", sup.calls[-1][1]) in sup.calls
    b = booking(ops_session, bid)
    assert (b.status, b.fare_paise) == (BookingStatus.HELD, 900_000)
    [ev] = events(ops_session, "fare_requoted")
    assert (ev.payload_json["old"], ev.payload_json["new"]) == (1_000_000, 900_000)
    assert n.posts[0][1] == "Optimization caught: fare dropped ₹1,000"


def test_held_drop_without_hold_support_asks_ops(ops_session):
    bid, offer = make_booking(ops_session, state="held", fare=1_000_000)
    sup = FakeSupplier(supports_hold=False, quotes={offer.offer_id: quote(offer, fare=900_000)})
    n = FakeNotifier()
    run_watchdog(sup, n, now=NOW, settings=cfg())
    assert booking(ops_session, bid).fare_paise == 1_000_000
    [ev] = events(ops_session, "fare_drop")
    assert "by hand" in ev.payload_json["detail"]
    assert len(n.posts) == 1


# --- schedule -----------------------------------------------------------------


def test_drift_dms_traveler_and_ops(ops_session):
    bid, offer = make_booking(ops_session)
    sup = FakeSupplier(quotes={offer.offer_id: quote(offer, dep=DEP - timedelta(minutes=35))})
    n = FakeNotifier()

    run_watchdog(sup, n, now=NOW, settings=cfg())

    msg = "DEL-BOM departure moved 35m earlier — check cab/meetings"
    assert [p[1] for p in n.posts] == [msg]
    assert [(d[0], d[1]) for d in n.dms] == [("UTRAV", msg)]
    detail = n.dms[0][2][0]["text"]["text"]
    assert "Departure: 01 Dec 05:30 IST (was 01 Dec 06:05)" in detail
    assert booking(ops_session, bid).depart_at == DEP - timedelta(minutes=35)


def test_drift_below_threshold_is_ignored(ops_session):
    _, offer = make_booking(ops_session)
    sup = FakeSupplier(quotes={offer.offer_id: quote(offer, arr=ARR + timedelta(minutes=19))})
    rep = run_watchdog(sup, FakeNotifier(), now=NOW, settings=cfg())
    assert rep.alerts == []


def test_supplier_booking_times_win_over_quote(ops_session):
    """A supplier that reports schedule times in get_booking (TBO) is believed first."""
    _, offer = make_booking(ops_session, ref="R1")
    sup = FakeSupplier(quotes={offer.offer_id: quote(offer)})
    sup.bookings["R1"] = BookingResult("ticketed", "R1", arrive_at=ARR + timedelta(hours=1, minutes=10))
    n = FakeNotifier()
    run_watchdog(sup, n, now=NOW, settings=cfg())
    assert n.posts[0][1] == "DEL-BOM arrival moved 1h 10m later — check cab/meetings"


# --- idempotency, scope, delivery --------------------------------------------


def test_rerun_does_not_alert_twice(ops_session):
    _, offer = make_booking(ops_session)
    sup = FakeSupplier(quotes={offer.offer_id: quote(offer, fare=900_000)})
    n = FakeNotifier()
    for i in range(3):
        run_watchdog(sup, n, now=NOW + timedelta(hours=3 * i), settings=cfg())
    assert len(n.posts) == 1
    assert len(events(ops_session, "fare_drop")) == 1
    with ops_session() as s:
        assert s.scalar(select(func.count()).select_from(SegmentSnapshot)) == 3


def test_recently_checked_booking_is_skipped(ops_session):
    _, offer = make_booking(ops_session)
    sup = FakeSupplier()
    run_watchdog(sup, FakeNotifier(), now=NOW, settings=cfg())
    rep = run_watchdog(sup, FakeNotifier(), now=NOW + timedelta(minutes=30), settings=cfg())
    assert rep.checked == 0
    assert [c[0] for c in sup.calls].count("price_check") == 1


def test_departed_and_cancelled_bookings_are_not_checked(ops_session):
    make_booking(ops_session, depart=NOW - timedelta(hours=1), ref="PAST")
    bid, _ = make_booking(ops_session, ref="CXL")
    with ops_session() as s:
        s.get(Booking, bid).status = BookingStatus.CANCELLED
    sup = FakeSupplier()
    rep = run_watchdog(sup, FakeNotifier(), now=NOW, settings=cfg())
    assert rep.checked == 0 and sup.calls == []


def test_failed_delivery_is_retried_only_for_missed_target(ops_session):
    _, offer = make_booking(ops_session)
    sup = FakeSupplier(quotes={offer.offer_id: quote(offer, dep=DEP + timedelta(minutes=40))})
    n = FakeNotifier(fail_targets={"UTRAV"})
    run_watchdog(sup, n, now=NOW, settings=cfg())
    assert len(n.posts) == 1 and n.dms == []

    n.fail_targets.clear()
    run_watchdog(sup, n, now=NOW + timedelta(hours=3), settings=cfg())
    assert len(n.posts) == 1  # ops not re-sent
    assert len(n.dms) == 1
    [ev] = events(ops_session, "schedule_drift")
    assert sorted(ev.payload_json["sent"]) == ["ops", "traveler:UTRAV"]


def test_one_failing_booking_does_not_stop_the_run(ops_session):
    _, bad = make_booking(ops_session, ref="BAD")
    _, good = make_booking(ops_session, ref="GOOD", fare=1_000_000)

    class Flaky(FakeSupplier):
        def price_check(self, offer, *, trace_id):
            if offer.offer_id == bad.offer_id:
                raise SupplierError("search down")
            return super().price_check(offer, trace_id=trace_id)

    sup = Flaky(quotes={good.offer_id: quote(good, fare=900_000)})
    rep = run_watchdog(sup, FakeNotifier(), now=NOW, settings=cfg())
    assert (rep.checked, rep.errors, len(rep.alerts)) == (1, 1, 1)


def test_booking_without_stored_offer_is_skipped_once(ops_session):
    bid, _ = make_booking(ops_session)
    with ops_session() as s:
        s.get(Booking, bid).offer_json = None
    for i in range(2):
        run_watchdog(FakeSupplier(), FakeNotifier(), now=NOW + timedelta(hours=3 * i), settings=cfg())
    assert len(events(ops_session, "watch_skipped")) == 1


def test_no_ops_channel_keeps_alert_pending(ops_session):
    _, offer = make_booking(ops_session)
    sup = FakeSupplier(quotes={offer.offer_id: quote(offer, fare=900_000)})
    n = FakeNotifier()
    run_watchdog(sup, n, now=NOW, settings=cfg(ops_slack_channel=""))
    assert n.posts == []
    run_watchdog(sup, n, now=NOW + timedelta(hours=3), settings=cfg())
    assert len(n.posts) == 1


# --- [Acknowledge] -------------------------------------------------------------


class FakeClient:
    def __init__(self):
        self.updates = []

    def chat_update(self, **kw):
        self.updates.append(kw)


def test_acknowledge_records_once_and_updates_message(ops_session):
    from traveldesk.ops.slack_actions import handle_ack

    _, offer = make_booking(ops_session)
    n = FakeNotifier()
    run_watchdog(
        FakeSupplier(quotes={offer.offer_id: quote(offer, fare=900_000)}), n, now=NOW, settings=cfg()
    )
    channel, text, blocks = n.posts[0]
    event_id = int(blocks[-1]["elements"][0]["value"])
    body = {
        "actions": [{"value": str(event_id)}],
        "user": {"id": "UOPS"},
        "channel": {"id": "C1"},
        "message": {"ts": "ts1", "text": text, "blocks": blocks},
    }
    client = FakeClient()

    assert handle_ack(body, client) is True
    assert handle_ack(replace_user(body, "UOTHER"), client) is False

    [ack] = events(ops_session, "acknowledged")
    assert ack.payload_json["user"] == "UOPS"
    alert = [e for e in events(ops_session) if e.id == event_id][0]
    assert alert.payload_json["acked_by"] == "UOPS"
    new_blocks = client.updates[0]["blocks"]
    assert all(b["type"] != "actions" for b in new_blocks)
    assert "Acknowledged by <@UOPS>" in new_blocks[-1]["elements"][0]["text"]


def replace_user(body, user):
    return {**body, "user": {"id": user}}


def test_register_wires_ack_action():
    from traveldesk.ops.slack_actions import register

    class FakeApp:
        def __init__(self):
            self.actions = {}

        def action(self, action_id):
            return lambda fn: self.actions.setdefault(action_id, fn)

    fake = FakeApp()
    register(fake)
    assert ACK_ACTION in fake.actions


def test_bot_app_registers_ops_actions(monkeypatch):
    from traveldesk.slack import app

    assert app._ops_slack_actions.ACK_ACTION == ACK_ACTION


# --- scheduler -----------------------------------------------------------------


def test_scheduler_job_settings():
    from traveldesk.ops.scheduler import build_scheduler

    sched = build_scheduler(cfg(watchdog_interval_hours=3, digest_time_ist="19:00"))
    jobs = {j.id: j for j in sched.get_jobs()}
    assert set(jobs) == {"watchdog", "digest", "group_poll"}
    assert jobs["watchdog"].trigger.interval == timedelta(hours=3)
    assert str(jobs["digest"].trigger.timezone) == "Asia/Kolkata"
    assert {f.name: str(f) for f in jobs["digest"].trigger.fields}["hour"] == "19"
    assert {f.name: str(f) for f in jobs["digest"].trigger.fields}["minute"] == "0"
    for j in jobs.values():
        assert (j.max_instances, j.coalesce) == (1, True)
