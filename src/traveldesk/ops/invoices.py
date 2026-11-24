"""Record a supplier invoice against a booking's debit (LetsFG sends invoices by email, not API).

    python -m traveldesk.ops.invoices attach <ops_booking_id> --invoice-no INV-123 \\
        --gstin 27AAPFU0939F1ZV --amount 10500.50

GST from the invoice (any of these recomputes the booking's GST; invoice heads are stored and
compared with the computed figure — off by more than ₹1 is flagged on the dashboard):

        --supplier-gstin 29AAGCB7383J1Z4 --invoice-date 2026-11-20 --taxable 10000 --igst 500 \\
        --invoice-url https://… [--category hotel --per-night 6800] [--cabin business]

    python -m traveldesk.ops.invoices itc-claimed <ops_booking_id>   # mark input tax credit claimed
"""

from __future__ import annotations

import argparse
from datetime import date
from decimal import Decimal, InvalidOperation

from traveldesk.ops import db as ops_db
from traveldesk.ops import gst
from traveldesk.ops.models import Booking, ItcStatus
from traveldesk.ops.records import apply_gst, attach_invoice
from traveldesk.ops.settings import get_settings
from traveldesk.ops.units import format_inr, to_paise


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m traveldesk.ops.invoices")
    sub = ap.add_subparsers(dest="command", required=True)
    a = sub.add_parser("attach", help="put invoice details on a booking's debit")
    a.add_argument("booking_id", type=int, help="ops_bookings.id (shown as #id by `digest --list`)")
    a.add_argument("--invoice-no", required=True)
    a.add_argument("--gstin", help="GSTIN the invoice is billed to")
    a.add_argument("--amount", help="invoice total in rupees, e.g. 10500.50")
    g = a.add_argument_group("GST")
    g.add_argument("--category", choices=gst.CATEGORIES)
    g.add_argument("--cabin", help="flight cabin, e.g. economy / business (default: the booked offer's)")
    g.add_argument("--per-night", type=_money, help="hotel tariff per night, ₹ (picks the 5%%/18%% slab)")
    g.add_argument("--taxable", type=_money, help="taxable value on the invoice, ₹")
    g.add_argument("--cgst", type=_money)
    g.add_argument("--sgst", type=_money)
    g.add_argument("--igst", type=_money)
    g.add_argument("--supplier-gstin")
    g.add_argument("--supplier-state", help="2-digit state code (default: from --supplier-gstin)")
    g.add_argument("--invoice-date", type=date.fromisoformat, help="YYYY-MM-DD")
    g.add_argument("--invoice-url")
    c = sub.add_parser("itc-claimed", help="mark a booking's input tax credit as claimed")
    c.add_argument("booking_id", type=int)
    args = ap.parse_args(argv)

    if args.command == "itc-claimed":
        with ops_db.session_scope() as s:
            b = s.get(Booking, args.booking_id)
            if b is None:
                raise SystemExit(f"no ops booking #{args.booking_id}")
            if b.itc_status is not ItcStatus.PENDING:
                raise SystemExit(f"#{b.id}: ITC is {b.itc_status or 'not computed'}, not pending")
            b.itc_status = ItcStatus.CLAIMED
            print(f"#{b.id}: ITC marked claimed")
        return

    for label, value in (("GSTIN", args.gstin), ("supplier GSTIN", args.supplier_gstin)):
        if value and not gst.is_valid(value):
            raise SystemExit(f"{label} {value!r} is not valid (format or check digit)")
    if args.category == "hotel" and args.per_night is None:
        raise SystemExit("--category hotel needs --per-night")
    amount = to_paise(args.amount) if args.amount else None
    heads = (args.cgst, args.sgst, args.igst)
    invoice_gst = None
    if any(h is not None for h in heads):
        invoice_gst = gst.InvoiceGst(*(h or Decimal("0") for h in heads))
    gst_given = any(
        v is not None
        for v in (args.category, args.cabin, args.per_night, args.taxable, invoice_gst, args.supplier_gstin,
                  args.supplier_state, args.invoice_date, args.invoice_url)
    )  # fmt: skip
    with ops_db.session_scope() as s:
        b = s.get(Booking, args.booking_id)
        if b is None:
            raise SystemExit(f"no ops booking #{args.booking_id}")
        if gst_given:
            cfg = get_settings()
            try:
                r = apply_gst(
                    s, b,
                    company_gstin=cfg.company_gstin, company_state=cfg.company_state_code,
                    category=args.category, taxable_value=args.taxable, cabin=args.cabin,
                    per_night_tariff=args.per_night, supplier_gstin=args.supplier_gstin,
                    supplier_state=args.supplier_state, invoice_no=args.invoice_no,
                    invoice_date=args.invoice_date, invoice_url=args.invoice_url, invoice_gst=invoice_gst,
                )  # fmt: skip
            except ValueError as err:
                raise SystemExit(str(err)) from err
            flag = f"  ≠ computed ₹{r.computed_gst} — flagged" if r.mismatch else ""
            print(f"#{b.id}: GST {r.gst_rate}% on ₹{r.taxable_value} = ₹{r.total_gst}{flag}")
        try:
            e = attach_invoice(
                s, b, invoice_no=args.invoice_no, gst_no=args.gstin, invoice_amount_paise=amount
            )
        except LookupError as err:
            raise SystemExit(str(err)) from err
        note = ""
        if e.invoice_amount_paise is not None and e.invoice_amount_paise != e.amount_paise:
            note = f"  (≠ debit {format_inr(e.amount_paise)} — will show in the digest)"
        print(f"#{b.id} {b.route} PNR {b.pnr}: invoice {e.invoice_no} saved{note}")


def _money(v: str) -> Decimal:
    try:
        d = Decimal(v.replace(",", ""))
    except InvalidOperation as e:
        raise argparse.ArgumentTypeError(f"not an amount: {v!r}") from e
    if not d.is_finite() or d < 0:
        raise argparse.ArgumentTypeError(f"not an amount: {v!r}")
    return gst.q2(d)


if __name__ == "__main__":
    main()
