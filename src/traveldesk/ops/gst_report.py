"""GST summary for a month / FY: `/traveldesk gst [month]` in Slack (dashboard /gst has the detail)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from urllib.parse import urlencode

from sqlalchemy import select
from sqlalchemy.orm import Session

from traveldesk.ops import db as ops_db
from traveldesk.ops import gst
from traveldesk.ops.models import Booking, BookingStatus, ItcStatus
from traveldesk.ops.settings import OpsSettings, get_settings
from traveldesk.ops.units import IST, format_inr, to_ist

# Money actually spent: ticketed / held, or a refund still pending (the tax is still on the invoice).
SPENT = (BookingStatus.TICKETED, BookingStatus.HELD, BookingStatus.REFUND_PENDING)


@dataclass
class Summary:
    label: str
    bookings: int = 0
    spend: int = 0  # paise: fares paid
    gst: int = 0
    cgst: int = 0
    sgst: int = 0
    igst: int = 0
    itc_eligible: int = 0
    itc_claimed: int = 0
    mismatches: int = 0
    missing_invoice: int = 0


def _ist_bounds(start: date, end: date) -> tuple[datetime, datetime]:
    return datetime.combine(start, time(), IST), datetime.combine(end, time(), IST)


def summarise(s: Session, start: date, end: date, label: str) -> Summary:
    lo, hi = _ist_bounds(start, end)
    rows = s.scalars(
        select(Booking).where(Booking.created_at >= lo, Booking.created_at < hi, Booking.status.in_(SPENT))
    ).all()
    out = Summary(label)
    for b in rows:
        if b.total_gst_paise is None:
            continue
        g = b.total_gst_paise
        out.bookings += 1
        out.spend += b.fare_paise
        out.gst += g
        out.cgst += b.cgst_paise or 0
        out.sgst += b.sgst_paise or 0
        out.igst += b.igst_paise or 0
        if b.itc_status in (ItcStatus.PENDING, ItcStatus.CLAIMED):
            out.itc_eligible += g
        if b.itc_status is ItcStatus.CLAIMED:
            out.itc_claimed += g
        out.mismatches += bool(b.gst_mismatch)
        out.missing_invoice += not b.invoice_no
    return out


def dashboard_link(cfg: OpsSettings, start: date, end: date) -> str | None:
    if not cfg.dashboard_url:
        return None
    if (end.year - start.year) * 12 + end.month - start.month == 12:
        q = {"fy": str(start.year)}
    else:
        q = {"month": start.strftime("%Y-%m")}
    return cfg.dashboard_url.rstrip("/") + "/gst?" + urlencode(q)


def slack_text(sm: Summary, link: str | None) -> str:
    if not sm.bookings:
        lines = [f"*GST · {sm.label}* — no bookings with GST yet."]
    else:
        lines = [
            f"*GST · {sm.label}* — {sm.bookings} booking{'s' if sm.bookings != 1 else ''}",
            f"Spend {format_inr(sm.spend)} · GST {format_inr(sm.gst)}"
            f" (CGST {format_inr(sm.cgst)} · SGST {format_inr(sm.sgst)} · IGST {format_inr(sm.igst)})",
            f"ITC eligible {format_inr(sm.itc_eligible)} · claimed {format_inr(sm.itc_claimed)}",
        ]
        warn = []
        if sm.mismatches:
            warn.append(f"{sm.mismatches} invoice GST mismatch{'es' if sm.mismatches != 1 else ''}")
        if sm.missing_invoice:
            warn.append(f"{sm.missing_invoice} without an invoice (estimated)")
        if warn:
            lines.append(":warning: " + " · ".join(warn))
    if link:
        lines.append(f"<{link}|Open GST dashboard>")
    return "\n".join(lines)


def for_slack(arg: str, today: date | None = None, cfg: OpsSettings | None = None) -> str:
    """Reply to `/traveldesk gst [month]`. A bad period gets a usage hint, not an error."""
    cfg = cfg or get_settings()
    today = today or to_ist(datetime.now(IST)).date()
    try:
        start, end, label = gst.parse_period(arg, today)
    except ValueError as e:
        return f"{e}. Usage: `/traveldesk gst [2026-11 | Nov | FY2026-27]`"
    with ops_db.session_scope() as s:
        sm = summarise(s, start, end, label)
    return slack_text(sm, dashboard_link(cfg, start, end))
