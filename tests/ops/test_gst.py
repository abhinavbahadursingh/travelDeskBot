"""GST: rate pick, CGST/SGST vs IGST split, invoice mismatch, periods, booking write path, Slack."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal as D

import pytest

from traveldesk.ops import gst, gst_report
from traveldesk.ops import settings as ops_settings
from traveldesk.ops.models import Booking, ItcStatus
from traveldesk.ops.records import apply_gst, create_request, record_booking, upsert_traveler
from traveldesk.ops.settings import OpsSettings
from traveldesk.ops.supplier.base import BookingResult

COMPANY = "27AAPFU0939F1ZV"  # Maharashtra
DAY = date(2026, 11, 20)


def gstin(state: str, body: str = "AAGCB7383J1Z") -> str:
    first14 = state + body
    return first14 + gst.check_digit(first14)


MH_SUPPLIER, KA_SUPPLIER = gstin("27"), gstin("29")


def versions(*vs: tuple[str, dict]) -> gst.Versions:
    return tuple((date.fromisoformat(eff), rates) for eff, rates in vs)


# --- rates ---------------------------------------------------------------------------


def test_rates_file_has_every_category_and_defaults_itc_to_false():
    eff, rates = gst.load_rates()[-1]
    assert set(rates) == {
        "flight_economy", "flight_premium", "hotel_upto_7500_per_night", "hotel_above_7500", "service_fee"
    }  # fmt: skip
    assert {k: v["rate"] for k, v in rates.items()} == {
        "flight_economy": "5", "flight_premium": "18", "hotel_upto_7500_per_night": "5",
        "hotel_above_7500": "18", "service_fee": "18",
    }  # fmt: skip
    assert not any(v["itc_eligible"] for v in rates.values())
    json.loads(gst.RATES_FILE.read_text())  # valid JSON


@pytest.mark.parametrize(
    ("category", "kw", "key", "rate"),
    [
        ("flight", {}, "flight_economy", "5"),
        ("flight", {"cabin": "economy"}, "flight_economy", "5"),
        ("flight", {"cabin": "premium_economy"}, "flight_premium", "18"),
        ("flight", {"cabin": "Business"}, "flight_premium", "18"),
        ("flight", {"cabin": "first"}, "flight_premium", "18"),
        ("flight", {"cabin": "C"}, "flight_premium", "18"),  # LetsFG cabin codes
        ("flight", {"cabin": "M"}, "flight_economy", "5"),
        ("hotel", {"per_night_tariff": D("7500")}, "hotel_upto_7500_per_night", "5"),
        ("hotel", {"per_night_tariff": D("7500.01")}, "hotel_above_7500", "18"),
        ("service_fee", {}, "service_fee", "18"),
    ],
)
def test_pick_rate(category, kw, key, rate):
    r = gst.pick_rate(category, DAY, **kw)
    assert (r.key, r.rate) == (key, D(rate))


def test_rate_version_by_booking_date():
    vs = versions(
        ("2024-01-01", {"service_fee": {"rate": "12"}}),
        ("2026-01-01", {"service_fee": {"rate": "18", "itc_eligible": True}}),
    )
    assert gst.pick_rate("service_fee", date(2025, 12, 31), versions=vs).rate == D("12")
    new = gst.pick_rate("service_fee", date(2026, 1, 1), versions=vs)
    assert (new.rate, new.itc_eligible, new.effective_from) == (D("18"), True, date(2026, 1, 1))
    assert gst.pick_rate("service_fee", date(2024, 6, 1), versions=vs).itc_eligible is False  # default
    with pytest.raises(ValueError, match="no GST rates in force"):
        gst.pick_rate("service_fee", date(2023, 12, 31), versions=vs)


def test_hotel_needs_tariff_and_unknown_category_fails():
    with pytest.raises(ValueError, match="per-night"):
        gst.pick_rate("hotel", DAY)
    with pytest.raises(ValueError, match="unknown GST category"):
        gst.pick_rate("train", DAY)


# --- split -------------------------------------------------------------------------------


def test_same_state_is_cgst_plus_sgst():
    r = gst.compute("flight", D("10000"), DAY, supplier_gstin=MH_SUPPLIER, company_gstin=COMPANY)
    assert (r.cgst, r.sgst, r.igst, r.total_gst) == (D("250.00"), D("250.00"), D("0.00"), D("500.00"))
    assert r.supplier_state == "27"


def test_other_state_is_igst():
    r = gst.compute("flight", D("10000"), DAY, supplier_gstin=KA_SUPPLIER, company_gstin=COMPANY)
    assert (r.cgst, r.sgst, r.igst) == (D("0.00"), D("0.00"), D("500.00"))


def test_unknown_supplier_state_is_igst():
    r = gst.compute("flight", D("10000"), DAY, company_gstin=COMPANY)
    assert r.igst == D("500.00") and r.supplier_state is None


def test_explicit_state_codes_beat_gstins():
    r = gst.compute(
        "flight", D("10000"), DAY, supplier_gstin=KA_SUPPLIER, supplier_state="7",
        company_gstin=COMPANY, company_state="07",
    )  # fmt: skip
    assert r.cgst == D("250.00") and r.supplier_state == "07"


def test_rounds_half_up_to_paise_per_head():
    # 18% of 1234.57 = 222.2226 → IGST 222.22; halves 111.1113 → 111.11 each
    inter = gst.compute("service_fee", D("1234.567"), DAY, company_gstin=COMPANY)
    assert inter.taxable_value == D("1234.57") and inter.igst == D("222.22")
    intra = gst.compute("service_fee", D("1234.57"), DAY, supplier_state="27", company_gstin=COMPANY)
    assert (intra.cgst, intra.sgst) == (D("111.11"), D("111.11"))
    # 5% of 10.10 = 0.505 → 0.51 (half-up, not banker's)
    assert gst.compute("flight", D("10.10"), DAY).igst == D("0.51")
    assert all(isinstance(x, D) for x in (inter.cgst, inter.igst, inter.total_gst))


def test_no_company_gstin_still_computes_and_itc_is_na():
    vs = versions(("2025-01-01", {"service_fee": {"rate": "18", "itc_eligible": True}}))
    r = gst.compute("service_fee", D("1000"), DAY, company_gstin=None, company_state="27",
                    supplier_state="27", versions=vs)  # fmt: skip
    assert r.total_gst == D("180.00") and r.cgst == D("90.00")
    assert r.itc_status == "na" and r.itc_eligible is True
    with_gstin = gst.compute("service_fee", D("1000"), DAY, company_gstin=COMPANY, versions=vs)
    assert with_gstin.itc_status == "pending"
    not_eligible = gst.compute("service_fee", D("1000"), DAY, company_gstin=COMPANY)
    assert not_eligible.itc_status == "na"


# --- invoice figures -------------------------------------------------------------------------


def test_invoice_gst_is_stored_and_small_difference_is_not_a_mismatch():
    inv = gst.InvoiceGst(igst=D("500.99"))
    r = gst.compute("flight", D("10000"), DAY, company_gstin=COMPANY, invoice=inv)
    assert (r.total_gst, r.igst, r.computed_gst, r.mismatch) == (D("500.99"), D("500.99"), D("500.00"), False)
    exactly_one = gst.compute("flight", D("10000"), DAY, invoice=gst.InvoiceGst(igst=D("499.00")))
    assert exactly_one.mismatch is False


def test_invoice_gst_off_by_more_than_a_rupee_is_flagged():
    r = gst.compute("flight", D("10000"), DAY, invoice=gst.InvoiceGst(igst=D("501.01")))
    assert r.mismatch is True and r.total_gst == D("501.01")
    # right total, wrong heads still totals the same → not a mismatch (heads are stored as printed)
    heads = gst.compute("flight", D("10000"), DAY, invoice=gst.InvoiceGst(cgst=D("250"), sgst=D("250")))
    assert heads.mismatch is False and heads.cgst == D("250.00")


def test_taxable_from_inclusive_and_paise_round_trip():
    assert gst.taxable_from_inclusive(D("10500"), D("5")) == D("10000.00")
    assert gst.to_paise(D("123.455")) == 12346
    assert gst.from_paise(12346) == D("123.46")


def test_cabin_of_finds_segment_cabin():
    raw = {"id": "o1", "outbound": {"segments": [{"cabin_class": "business"}]}}
    assert gst.cabin_of(raw) == "business"
    assert gst.cabin_of({"id": "o1"}) is None and gst.cabin_of(None) is None


def test_state_of():
    assert gst.state_of(COMPANY) == "27"
    assert gst.state_of("") is None and gst.state_of("short") is None


# --- periods ------------------------------------------------------------------------------------

TODAY = date(2026, 11, 23)


@pytest.mark.parametrize(
    ("text", "start", "end", "label"),
    [
        ("", date(2026, 11, 1), date(2026, 12, 1), "Nov 2026"),
        ("2026-10", date(2026, 10, 1), date(2026, 11, 1), "Oct 2026"),
        ("3/2026", date(2026, 3, 1), date(2026, 4, 1), "Mar 2026"),
        ("dec", date(2025, 12, 1), date(2026, 1, 1), "Dec 2025"),  # most recent December
        ("November", date(2026, 11, 1), date(2026, 12, 1), "Nov 2026"),
        ("Dec 2026", date(2026, 12, 1), date(2027, 1, 1), "Dec 2026"),
        ("fy", date(2026, 4, 1), date(2027, 4, 1), "FY 2026-27"),
        ("FY2025-26", date(2025, 4, 1), date(2026, 4, 1), "FY 2025-26"),
        ("fy25", date(2025, 4, 1), date(2026, 4, 1), "FY 2025-26"),
    ],
)
def test_parse_period(text, start, end, label):
    assert gst.parse_period(text, TODAY) == (start, end, label)


@pytest.mark.parametrize("text", ["2026-13", "smarch", "fy banana", "next week"])
def test_parse_period_rejects_junk(text):
    with pytest.raises(ValueError):
        gst.parse_period(text, TODAY)


def test_fy_of():
    assert gst.fy_of(date(2026, 3, 31)) == 2025 and gst.fy_of(date(2026, 4, 1)) == 2026


# --- booking write path ------------------------------------------------------------------------------


@pytest.fixture
def cfg(monkeypatch):
    c = OpsSettings(company_gstin=COMPANY, dashboard_url="http://dash.local:5173/")
    monkeypatch.setattr(ops_settings, "get_settings", lambda: c)
    monkeypatch.setattr(gst_report, "get_settings", lambda: c)
    return c


def _ticket(ops_session, fare=1_050_000, created=datetime(2026, 11, 20, 6, 0, tzinfo=UTC)):
    with ops_session() as s:
        t = upsert_traveler(s, slack_id="UT", name="Asha Rao")
        r = create_request(s, slack_user="UT", raw_text="x")
        b = record_booking(
            s, r, supplier="fake", traveler=t, route="DEL-BOM",
            result=BookingResult("ticketed", f"REF{created.timestamp()}{fare}", pnr="PNR1", fare_paise=fare),
        )  # fmt: skip
        b.created_at = created
        return b.id


def test_ticketing_stores_a_gst_estimate(ops_session, cfg):
    bid = _ticket(ops_session)
    with ops_session() as s:
        b = s.get(Booking, bid)
        # ₹10,500 inclusive at 5% → taxable 10,000, IGST 500 (supplier state unknown)
        assert (b.category, b.taxable_value_paise, b.gst_rate) == ("flight", 1_000_000, D("5"))
        assert (b.igst_paise, b.total_gst_paise, b.gst_mismatch) == (50_000, 50_000, False)
        assert b.itc_status is ItcStatus.NA and b.invoice_no is None


def test_invoice_replaces_estimate_and_flags_mismatch(ops_session, cfg):
    bid = _ticket(ops_session)
    with ops_session() as s:
        b = s.get(Booking, bid)
        apply_gst(
            s, b, company_gstin=COMPANY, supplier_gstin=MH_SUPPLIER, taxable_value=D("10000"),
            invoice_no="INV-9", invoice_date=date(2026, 11, 20), invoice_url="https://x/inv.pdf",
            invoice_gst=gst.InvoiceGst(cgst=D("260"), sgst=D("260")),
        )  # fmt: skip
    with ops_session() as s:
        b = s.get(Booking, bid)
        assert (b.cgst_paise, b.sgst_paise, b.igst_paise) == (26_000, 26_000, 0)
        assert (b.total_gst_paise, b.computed_gst_paise, b.gst_mismatch) == (52_000, 50_000, True)
        assert (b.supplier_gstin, b.supplier_state, b.invoice_no) == (MH_SUPPLIER, "27", "INV-9")
        # a later recompute without invoice figures keeps the flag and the invoice fields
        apply_gst(s, b, company_gstin=COMPANY)
        assert b.gst_mismatch is True and b.invoice_url == "https://x/inv.pdf"


def test_claimed_itc_survives_a_recompute(ops_session, cfg):
    bid = _ticket(ops_session)
    with ops_session() as s:
        b = s.get(Booking, bid)
        b.itc_status = ItcStatus.CLAIMED
        apply_gst(s, b, company_gstin=COMPANY)
        assert b.itc_status is ItcStatus.CLAIMED


def test_invoice_cli_gst_options(ops_session, cfg, capsys):
    from traveldesk.ops.invoices import main

    bid = _ticket(ops_session)
    main([
        "attach", str(bid), "--invoice-no", "INV-5", "--supplier-gstin", KA_SUPPLIER,
        "--taxable", "10,000", "--igst", "503", "--invoice-date", "2026-11-21",
    ])  # fmt: skip
    out = capsys.readouterr().out
    assert "GST 5% on ₹10000.00 = ₹503.00  ≠ computed ₹500.00 — flagged" in out
    with pytest.raises(SystemExit, match="ITC is na"):
        main(["itc-claimed", str(bid)])
    with pytest.raises(SystemExit, match="not valid"):
        main(["attach", str(bid), "--invoice-no", "X", "--supplier-gstin", "29AAGCB7383J1Z0"])


# --- Slack summary ------------------------------------------------------------------------------------


def test_slack_summary_for_month(ops_session, cfg):
    a = _ticket(ops_session)
    _ticket(ops_session, fare=2_100_000, created=datetime(2026, 10, 31, 19, 0, tzinfo=UTC))  # 1 Nov IST
    _ticket(ops_session, fare=999_00, created=datetime(2026, 10, 31, 18, 0, tzinfo=UTC))  # 31 Oct IST
    with ops_session() as s:
        apply_gst(
            s,
            s.get(Booking, a),
            company_gstin=COMPANY,
            invoice_no="I1",
            invoice_gst=gst.InvoiceGst(igst=D("510")),
        )
    text = gst_report.for_slack("2026-11", today=TODAY)
    assert "*GST · Nov 2026* — 2 bookings" in text
    assert "Spend ₹31,500 · GST ₹1,510 (CGST ₹0 · SGST ₹0 · IGST ₹1,510)" in text
    assert "1 invoice GST mismatch" in text and "1 without an invoice" in text
    assert "<http://dash.local:5173/gst?month=2026-11|Open GST dashboard>" in text


def test_slack_summary_fy_link_and_bad_period(ops_session, cfg):
    text = gst_report.for_slack("fy", today=TODAY)
    assert "FY 2026-27* — no bookings" in text and "/gst?fy=2026|" in text
    assert "Usage: `/traveldesk gst" in gst_report.for_slack("blah", today=TODAY)
