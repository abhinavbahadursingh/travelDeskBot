"""Phase 3 — end-of-day leakage digest (daily at DIGEST_TIME_IST, posted to OPS_SLACK_CHANNEL).

Buckets (each booking appears in at most one, first match wins):
  0. charged_no_ticket — booking-time (pending) debit older than TICKET_TIMEOUT and still no PNR
  0. awaiting_payment  — supplier pay link unpaid for more than AWAITING_PAYMENT_ALERT (30 min)
  1. unbilled      — ticketed, but no ledger debit, or the debit has no invoice number
  2. refund_stale  — refund_pending for more than REFUND_PENDING_MAX_DAYS
  3. gst           — debit with an invoice whose GSTIN is missing/invalid/not the company's,
                     or whose invoice amount ≠ the debit
Items someone marked resolved (event `leak_resolved`) stay hidden. Empty buckets are skipped;
no items at all → "All clear". One digest per IST day (event `digest`, dedupe by date).

    python -m traveldesk.ops.digest --list   # print today's items
    python -m traveldesk.ops.digest --post   # post now (still once per day)
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from traveldesk.ops import db as ops_db
from traveldesk.ops import gst
from traveldesk.ops.models import Booking, BookingStatus, Event, LedgerStatus, LedgerType
from traveldesk.ops.notify import Notifier
from traveldesk.ops.records import add_event
from traveldesk.ops.settings import OpsSettings, get_settings
from traveldesk.ops.units import IST, fmt_ist, format_inr, utcnow

log = logging.getLogger("traveldesk.ops.digest")

RESOLVE_ACTION = "ops_leak_resolve"
OPEN_ACTION = "ops_open_booking"
OPEN_PAY_ACTION = "ops_open_pay_link"
SLACK_MAX_BLOCKS = 50


@dataclass(frozen=True)
class Bucket:
    key: str
    title: str


def buckets(cfg: OpsSettings) -> list[Bucket]:
    return [
        Bucket("charged_no_ticket", "Charged, no ticket"),
        Bucket("awaiting_payment", "Awaiting payment"),
        Bucket("unbilled", "Ticketed, no ledger debit or invoice"),
        Bucket("refund_stale", f"Refund pending > {cfg.refund_pending_max_days} days"),
        Bucket("gst", "GST missing/mismatched or invoice ≠ debit"),
    ]


@dataclass(frozen=True)
class LeakItem:
    bucket: str
    booking_id: int
    summary: str
    legacy_booking_id: int | None = None
    pay_url: str | None = None


def _age(td: timedelta) -> str:
    minutes = max(0, int(td.total_seconds() // 60))
    h, m = divmod(minutes, 60)
    return f"{h}h {m}m" if h and m else (f"{h}h" if h else f"{m}m")


def _resolved(s) -> set[tuple[str, int]]:
    out = set()
    for key in s.scalars(select(Event.dedupe_key).where(Event.kind == "leak_resolved")):
        _, bucket, bid = (key or "::").split(":", 2)
        if bid.isdigit():
            out.add((bucket, int(bid)))
    return out


def _who(b: Booking) -> str:
    return b.traveler.name if b.traveler else b.request.slack_user


def _line(b: Booking, problem: str) -> str:
    when = fmt_ist(b.depart_at, "%d %b") if b.depart_at else "date ?"
    return f"{b.route} {when} · PNR {b.pnr or '—'} · {_who(b)} · {format_inr(b.fare_paise)} — {problem}"


def _gst_problems(b: Booking, cfg: OpsSettings) -> list[str]:
    company = gst.normalize(cfg.company_gstin)
    problems = []
    for e in b.ledger:
        if e.type is not LedgerType.DEBIT or e.status is LedgerStatus.VOID:
            continue
        g = gst.normalize(e.gst_no)
        if not g:
            problems.append(f"invoice {e.invoice_no}: GSTIN missing")
        elif not gst.is_valid(g):
            problems.append(f"invoice {e.invoice_no}: GSTIN {g} invalid")
        elif company and g != company:
            problems.append(f"invoice {e.invoice_no}: billed to {g}, not company GSTIN")
        if e.invoice_amount_paise is not None and e.invoice_amount_paise != e.amount_paise:
            problems.append(
                f"invoice {format_inr(e.invoice_amount_paise)} ≠ debit {format_inr(e.amount_paise)}"
            )
    return problems


def find_leaks(now: datetime | None = None, cfg: OpsSettings | None = None) -> dict[str, list[LeakItem]]:
    cfg = cfg or get_settings()
    now = now or utcnow()
    stale_before = now - timedelta(days=cfg.refund_pending_max_days)
    found: dict[str, list[LeakItem]] = {bk.key: [] for bk in buckets(cfg)}

    with ops_db.session_scope() as s:
        hidden = _resolved(s)
        rows = s.scalars(
            select(Booking)
            .where(
                Booking.status.in_(
                    [
                        BookingStatus.PENDING,
                        BookingStatus.AWAITING_PAYMENT,
                        BookingStatus.TICKETED,
                        BookingStatus.REFUND_PENDING,
                    ]
                )
            )
            .options(
                selectinload(Booking.ledger), selectinload(Booking.traveler), selectinload(Booking.request)
            )
            .order_by(Booking.depart_at, Booking.id)
        ).all()

        for b in rows:
            item = None
            if b.status is BookingStatus.PENDING:
                charged = [
                    e for e in b.ledger
                    if e.type is LedgerType.DEBIT and e.status is LedgerStatus.PENDING
                    and e.created_at < now - cfg.ticket_timeout
                ]  # fmt: skip
                if charged and not b.pnr:
                    e = charged[0]
                    item = (
                        "charged_no_ticket",
                        f"charged {format_inr(e.amount_paise)} {_age(now - e.created_at)} ago, no ticket "
                        f"(ref {b.supplier_ref})",
                    )
            elif b.status is BookingStatus.AWAITING_PAYMENT:
                since = b.status_changed_at or b.created_at
                if since < now - cfg.awaiting_payment_alert:
                    if b.pay_expires_at and b.pay_expires_at <= now:
                        state = "link expired"
                    elif b.pay_expires_at:
                        state = f"link valid until {fmt_ist(b.pay_expires_at, '%d %b %H:%M')} IST"
                    else:
                        state = "no expiry given"
                    item = ("awaiting_payment", f"pay link unpaid for {_age(now - since)} — {state}")
            elif b.status is BookingStatus.TICKETED:
                debits = [
                    e for e in b.ledger if e.type is LedgerType.DEBIT and e.status is not LedgerStatus.VOID
                ]
                if not debits:
                    item = ("unbilled", "no ledger debit")
                elif any(not (e.invoice_no or "").strip() for e in debits):
                    item = ("unbilled", "no invoice number")
                elif problems := _gst_problems(b, cfg):
                    item = ("gst", "; ".join(problems))
            elif b.status_changed_at is None or b.status_changed_at < stale_before:
                days = (now - b.status_changed_at).days if b.status_changed_at else None
                item = (
                    "refund_stale",
                    f"refund pending {days} days" if days is not None else "refund pending (age unknown)",
                )
            if item and (item[0], b.id) not in hidden:
                legacy = b.request.legacy_booking_id if b.request else None
                found[item[0]].append(LeakItem(item[0], b.id, _line(b, item[1]), legacy, b.pay_url))
    return found


def _item_blocks(item: LeakItem, cfg: OpsSettings) -> list[dict]:
    resolve = {
        "type": "button",
        "action_id": RESOLVE_ACTION,
        "text": {"type": "plain_text", "text": "Mark resolved"},
        "value": f"{item.bucket}:{item.booking_id}",
    }
    buttons: list[dict] = [resolve]
    if item.bucket == "awaiting_payment":
        buttons = [resolve]
        if item.pay_url:
            pay = {"type": "button", "action_id": OPEN_PAY_ACTION, "url": item.pay_url,
                   "text": {"type": "plain_text", "text": "Open pay link"}}  # fmt: skip
            buttons = [pay, resolve]
    elif cfg.dashboard_url and item.legacy_booking_id:
        buttons.append(
            {
                "type": "button",
                "action_id": OPEN_ACTION,
                "text": {"type": "plain_text", "text": "Open booking"},
                "url": f"{cfg.dashboard_url.rstrip('/')}/?booking={item.legacy_booking_id}",
            }
        )
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": item.summary}},
        {"type": "actions", "block_id": f"leak:{item.bucket}:{item.booking_id}", "elements": buttons},
    ]


def build_message(
    found: dict[str, list[LeakItem]], now: datetime, cfg: OpsSettings
) -> tuple[str, list[dict]]:
    day = now.astimezone(IST).strftime("%a %d %b")
    total = sum(len(v) for v in found.values())
    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": f"Travel desk leakage — {day}"}}
    ]
    if not total:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": ":white_check_mark: *All clear* — nothing leaking today."},
            }
        )
        return f"Travel desk leakage — {day}: all clear", blocks

    # Slack: max 50 blocks. 1 header + per non-empty bucket (title + overflow line + 2 per item).
    shown = sum(1 for v in found.values() if v)
    cap = max(1, min(cfg.digest_max_items_per_bucket, (SLACK_MAX_BLOCKS - 1 - 2 * shown) // (2 * shown)))
    parts = []
    n = 0
    for bk in buckets(cfg):
        items = found.get(bk.key) or []
        if not items:
            continue
        n += 1  # numbered in the order shown
        parts.append(f"{len(items)} {bk.key.replace('_', ' ')}")
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": f"*{n}. {bk.title}* ({len(items)})"}}
        )
        for item in items[:cap]:
            blocks += _item_blocks(item, cfg)
        if len(items) > cap:
            more = len(items) - cap
            blocks.append(
                {
                    "type": "context",
                    "elements": [
                        {"type": "mrkdwn", "text": f"+{more} more — `python -m traveldesk.ops.digest --list`"}
                    ],
                }
            )
    return f"Travel desk leakage — {day}: {total} item(s) ({', '.join(parts)})", blocks


def post_digest(
    notifier: Notifier, *, now: datetime | None = None, cfg: OpsSettings | None = None
) -> str | None:
    """Post today's digest once. Returns the message ts, or None if already posted / not posted."""
    cfg = cfg or get_settings()
    now = now or utcnow()
    day = now.astimezone(IST).date().isoformat()
    key = f"digest:{day}"
    if not cfg.ops_slack_channel:
        log.warning("OPS_SLACK_CHANNEL not set; digest for %s not posted", day)
        return None

    with ops_db.session_scope() as s:
        ev = s.scalar(select(Event).where(Event.dedupe_key == key))
        if ev is not None and (ev.payload_json or {}).get("sent"):
            log.info("digest for %s already posted", day)
            return None

    found = find_leaks(now, cfg)
    text, blocks = build_message(found, now, cfg)
    ts = notifier.post(cfg.ops_slack_channel, text, blocks)  # raises → not marked sent → next run retries

    counts = {k: len(v) for k, v in found.items()}
    with ops_db.session_scope() as s:
        ev = s.scalar(select(Event).where(Event.dedupe_key == key))
        payload = {"day": day, "counts": counts, "sent": True, "ts": ts}
        if ev is None:
            add_event(s, None, "digest", payload, dedupe_key=key, created_at=now)
        else:
            ev.payload_json = payload
    log.info("digest for %s posted: %s", day, counts)
    return ts


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(prog="python -m traveldesk.ops.digest")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--list", action="store_true", help="print today's items")
    g.add_argument("--post", action="store_true", help="post today's digest now (once per day)")
    args = ap.parse_args(argv)
    cfg = get_settings()
    if args.list:
        found = find_leaks(cfg=cfg)
        for bk in buckets(cfg):
            items = found[bk.key]
            print(f"{bk.title} ({len(items)})")
            for it in items:
                print(f"  #{it.booking_id}  {it.summary}")
        return
    from traveldesk.ops.scheduler import make_notifier

    post_digest(make_notifier(cfg), cfg=cfg)


if __name__ == "__main__":
    main()
