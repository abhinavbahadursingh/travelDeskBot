"""Phase 2 — fare-drop and schedule-drift watchdog (runs every WATCHDOG_INTERVAL_HOURS).

For each held/ticketed booking that hasn't departed:
  1. price_check (and get_booking, for suppliers that report schedule times) — outside any DB transaction.
  2. Store an ops_segments_snapshot.
  3. Fare drop ≥ FARE_DROP_THRESHOLD_PAISE:
       held      → requote_hold at the lower fare; event `fare_requoted`
                   (supplier can't re-quote → event `fare_drop`, ops told to do it by hand)
       ticketed + refundable → event `fare_drop`, notify only
       ticketed, not refundable / unknown → nothing
  4. Departure or arrival moved ≥ SCHEDULE_DRIFT_MINUTES → event `schedule_drift`; DM traveller + ops.

Idempotent: alert events carry a dedupe key (booking + new fare / new times), so a re-run
never alerts twice for the same change. Delivery is separate from detection: alerts are
written first and then sent by `deliver_pending`, which records each target it reached and
retries the rest on the next run.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

from sqlalchemy import exists, select

from traveldesk.ops import db as ops_db
from traveldesk.ops.models import Booking, BookingStatus, Event, SegmentSnapshot
from traveldesk.ops.notify import Notifier, alert_blocks
from traveldesk.ops.records import add_event
from traveldesk.ops.settings import OpsSettings, get_settings
from traveldesk.ops.supplier.base import (
    BookingResult,
    PriceQuote,
    Supplier,
    SupplierError,
    SupplierUnsupported,
    new_trace_id,
    offer_from_json,
)
from traveldesk.ops.units import fmt_ist, format_inr, utcnow

log = logging.getLogger("traveldesk.ops.watchdog")

ALERT_KINDS = ("fare_requoted", "fare_drop", "schedule_drift")
OPS = "ops"
DELIVERY_WINDOW = timedelta(hours=48)  # stop retrying an alert's delivery after this


@dataclass
class WatchReport:
    trace_id: str
    checked: int = 0
    errors: int = 0
    alerts: list[int] = field(default_factory=list)  # new alert event ids
    delivered: int = 0


def due_bookings(now: datetime, min_recheck: timedelta) -> list[int]:
    """Held/ticketed, departing in the future, not re-checked within `min_recheck`."""
    recent = exists().where(
        SegmentSnapshot.booking_id == Booking.id, SegmentSnapshot.captured_at > now - min_recheck
    )
    with ops_db.session_scope() as s:
        return list(
            s.scalars(
                select(Booking.id)
                .where(
                    Booking.status.in_([BookingStatus.HELD, BookingStatus.TICKETED]),
                    Booking.depart_at > now,
                    ~recent,
                )
                .order_by(Booking.depart_at)
            )
        )


def run_watchdog(
    supplier: Supplier,
    notifier: Notifier,
    *,
    now: datetime | None = None,
    settings: OpsSettings | None = None,
) -> WatchReport:
    cfg = settings or get_settings()
    now = now or utcnow()
    report = WatchReport(trace_id=new_trace_id())
    ids = due_bookings(now, timedelta(minutes=cfg.watchdog_min_recheck_minutes))
    log.info("watchdog trace_id=%s due=%d", report.trace_id, len(ids))
    for bid in ids:
        try:
            report.alerts += check_booking(bid, supplier, now=now, cfg=cfg, trace_id=report.trace_id)
            report.checked += 1
        except Exception:  # noqa: BLE001 — one bad booking/supplier error must not stop the run
            report.errors += 1
            log.exception("watchdog trace_id=%s booking=%s failed", report.trace_id, bid)
    report.delivered = deliver_pending(notifier, now=now, cfg=cfg)
    log.info(
        "watchdog trace_id=%s checked=%d errors=%d alerts=%d delivered=%d",
        report.trace_id,
        report.checked,
        report.errors,
        len(report.alerts),
        report.delivered,
    )
    return report


def check_booking(
    bid: int, supplier: Supplier, *, now: datetime, cfg: OpsSettings, trace_id: str
) -> list[int]:
    """Re-check one booking. Returns ids of newly created alert events."""
    with ops_db.session_scope() as s:
        b = s.get(Booking, bid)
        if b is None or b.supplier != supplier.name:
            return []
        if not b.offer_json:
            add_event(
                s,
                b,
                "watch_skipped",
                {"reason": "no offer stored"},
                dedupe_key=f"watch_skipped:{b.id}",
                created_at=now,
            )
            return []
        offer = offer_from_json(b.offer_json)
        ref, status, fare_paid = b.supplier_ref, b.status, b.fare_paise

    # --- supplier calls, no DB transaction open ---
    quote = supplier.price_check(offer, trace_id=trace_id)
    info: BookingResult | None = None
    try:
        info = supplier.get_booking(ref, trace_id=trace_id)
    except (SupplierError, SupplierUnsupported):
        info = None
    requoted: BookingResult | None = None
    drop = (fare_paid - quote.fare_paise) if quote.available and quote.fare_paise is not None else 0
    if status is BookingStatus.HELD and drop >= cfg.fare_drop_threshold_paise:
        target = quote.offer or replace(offer, fare_paise=quote.fare_paise)
        try:
            requoted = supplier.requote_hold(ref, target, trace_id=trace_id)
        except SupplierUnsupported:
            requoted = None

    new_dep, new_arr = _schedule(info, quote)

    with ops_db.session_scope() as s:
        b = s.get(Booking, bid)
        s.add(
            SegmentSnapshot(
                booking_id=b.id,
                depart_at=new_dep,
                arrive_at=new_arr,
                fare_paise=quote.fare_paise if quote.available else None,
                captured_at=now,
            )
        )
        if b.status not in (BookingStatus.HELD, BookingStatus.TICKETED):
            return []  # cancelled while we were checking
        alerts: list[Event | None] = []
        if not quote.available:
            add_event(
                s,
                b,
                "flight_not_found",
                {"trace_id": trace_id},
                dedupe_key=f"not_found:{b.id}:{now.date()}",
                created_at=now,
            )
        alerts.append(_fare_rules(s, b, quote, drop, requoted, cfg, trace_id, now))
        alerts.append(_drift_rules(s, b, new_dep, new_arr, cfg, trace_id, now))
        return [e.id for e in alerts if e is not None]


def _schedule(info: BookingResult | None, quote: PriceQuote) -> tuple[datetime | None, datetime | None]:
    """Supplier's booking times win; else the re-searched flight's times."""
    dep = info.depart_at if info and info.depart_at else (quote.depart_at if quote.available else None)
    arr = info.arrive_at if info and info.arrive_at else (quote.arrive_at if quote.available else None)
    return dep, arr


def _fare_rules(
    s, b: Booking, quote: PriceQuote, drop: int, requoted, cfg: OpsSettings, trace_id: str, now: datetime
):
    if drop < cfg.fare_drop_threshold_paise:
        return None
    old, new = b.fare_paise, quote.fare_paise
    headline = f"Optimization caught: fare dropped {format_inr(drop)}"
    trip = f"{b.route} · {fmt_ist(b.depart_at)} IST · PNR {b.pnr or '—'}"

    if b.status is BookingStatus.HELD and requoted is not None and requoted.state == "held":
        new = requoted.fare_paise if requoted.fare_paise is not None else new
        detail = f"{trip}\nHeld booking re-quoted: {format_inr(old)} → {format_inr(new)}."
        ev = _alert(s, b, "fare_requoted", headline, detail, [OPS], f"fare_requoted:{b.id}:{new}",
                    old=old, new=new, trace_id=trace_id, now=now)  # fmt: skip
        if ev is not None:
            b.fare_paise = new
            if requoted.supplier_ref and requoted.supplier_ref != b.supplier_ref:
                b.supplier_ref = requoted.supplier_ref  # a re-hold can issue a new reference
        return ev
    if b.status is BookingStatus.HELD:
        detail = (
            f"{trip}\nHeld at {format_inr(old)}, now {format_inr(new)}. "
            f"{b.supplier} can't re-quote a hold automatically — re-quote it by hand."
        )
    elif b.refundable is True:
        detail = (
            f"{trip}\nTicketed at {format_inr(old)}, now {format_inr(new)}. Refundable fare: "
            "cancel & rebook may save money after cancellation fees. Not done automatically."
        )
    else:
        return None
    return _alert(s, b, "fare_drop", headline, detail, [OPS], f"fare_drop:{b.id}:{new}",
                  old=old, new=new, trace_id=trace_id, now=now)  # fmt: skip


def _shift_text(minutes: int) -> str:
    m = abs(minutes)
    span = f"{m // 60}h {m % 60}m" if m >= 60 and m % 60 else (f"{m // 60}h" if m >= 60 else f"{m}m")
    return f"{span} {'earlier' if minutes < 0 else 'later'}"


def _drift_rules(s, b: Booking, new_dep, new_arr, cfg: OpsSettings, trace_id: str, now: datetime):
    shifts = []
    for label, old, new in (("Departure", b.depart_at, new_dep), ("Arrival", b.arrive_at, new_arr)):
        if old and new:
            minutes = round((new - old).total_seconds() / 60)
            if abs(minutes) >= cfg.schedule_drift_minutes:
                shifts.append((label, minutes, old, new))
    if not shifts:
        # Fill in times we didn't know yet (legacy rows often lack arrival) without alerting.
        b.depart_at = b.depart_at or new_dep
        b.arrive_at = b.arrive_at or new_arr
        return None

    label, minutes, _, _ = shifts[0]
    headline = f"{b.route} {label.lower()} moved {_shift_text(minutes)} — check cab/meetings"
    lines = [f"{lbl}: {fmt_ist(new)} IST (was {fmt_ist(old)})" for lbl, _, old, new in shifts]
    detail = f"PNR {b.pnr or '—'}\n" + "\n".join(lines)
    targets = [OPS] + ([f"traveler:{b.traveler.slack_id}"] if b.traveler and b.traveler.slack_id else [])
    key = f"drift:{b.id}:{new_dep.isoformat() if new_dep else '-'}:{new_arr.isoformat() if new_arr else '-'}"
    ev = _alert(s, b, "schedule_drift", headline, detail, targets, key,
                shifts={lbl: m for lbl, m, _, _ in shifts}, trace_id=trace_id, now=now)  # fmt: skip
    if ev is not None:
        b.depart_at = new_dep or b.depart_at
        b.arrive_at = new_arr or b.arrive_at
    return ev


def _alert(
    s, b: Booking, kind: str, text: str, detail: str, targets: list[str], key: str, *, now: datetime, **extra
):
    payload = {"text": text, "detail": detail, "targets": targets, "sent": [], "messages": [], **extra}
    return add_event(s, b, kind, payload, dedupe_key=key, created_at=now)


def deliver_pending(notifier: Notifier, *, now: datetime, cfg: OpsSettings) -> int:
    """Send every alert target not yet reached (recent alerts only). Returns messages sent."""
    sent = 0
    with ops_db.session_scope() as s:
        events = s.scalars(
            select(Event)
            .where(Event.kind.in_(ALERT_KINDS), Event.created_at > now - DELIVERY_WINDOW)
            .order_by(Event.id)
        ).all()
        for ev in events:
            p = dict(ev.payload_json or {})
            done, messages = list(p.get("sent") or []), list(p.get("messages") or [])
            for target in p.get("targets") or []:
                if target in done:
                    continue
                blocks = alert_blocks(p["text"], p["detail"], ev.id)
                try:
                    if target == OPS:
                        if not cfg.ops_slack_channel:
                            log.warning("OPS_SLACK_CHANNEL not set; ops alert %s not sent", ev.id)
                            continue
                        ts = notifier.post(cfg.ops_slack_channel, p["text"], blocks)
                    else:
                        ts = notifier.dm(target.split(":", 1)[1], p["text"], blocks)
                except Exception:  # noqa: BLE001 — retry on the next run
                    log.exception("alert %s → %s failed", ev.id, target)
                    continue
                done.append(target)
                messages.append({"target": target, "ts": ts})
                sent += 1
            ev.payload_json = {
                **p,
                "sent": done,
                "messages": messages,
            }  # reassign: JSON isn't mutation-tracked
    return sent
