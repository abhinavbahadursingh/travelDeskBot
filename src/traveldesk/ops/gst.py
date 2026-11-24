"""GST: GSTIN validation, rate lookup and tax split. Pure functions (no DB, no settings).

Money is Decimal rupees rounded half-up to 2 dp; callers store it as integer paise.
Rates come from gst_rates.json, versioned by effective_from date.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any

_FORMAT = re.compile(r"^[0-3][0-9][A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"

RATES_FILE = Path(__file__).with_name("gst_rates.json")
CATEGORIES = ("flight", "hotel", "service_fee")
PREMIUM_CABINS = {"premium_economy", "premium", "business", "first", "w", "c", "j", "f"}
HOTEL_SLAB = Decimal("7500")  # per-night tariff up to and including this → lower rate
MISMATCH_TOLERANCE = Decimal("1")  # ₹; invoice GST further than this from computed is flagged
CENT = Decimal("0.01")
Versions = tuple[tuple[date, dict[str, Any]], ...]


def check_digit(first14: str) -> str:
    total = 0
    for i, ch in enumerate(first14):
        v = _CHARS.index(ch) * (2 if i % 2 else 1)
        total += v // 36 + v % 36
    return _CHARS[(36 - total % 36) % 36]


def normalize(gstin: str | None) -> str:
    return (gstin or "").strip().upper()


def is_valid(gstin: str | None) -> bool:
    g = normalize(gstin)
    return bool(_FORMAT.match(g)) and check_digit(g[:14]) == g[14]


def state_of(gstin: str | None) -> str | None:
    """State code = the GSTIN's first two digits ('27' = Maharashtra). None if not a GSTIN."""
    g = normalize(gstin)
    return g[:2] if len(g) == 15 and g[:2].isdigit() else None


# --- money ---------------------------------------------------------------------


def q2(x: Decimal | int | str) -> Decimal:
    return Decimal(str(x)).quantize(CENT, rounding=ROUND_HALF_UP)


def to_paise(x: Decimal) -> int:
    return int(q2(x) * 100)


def from_paise(p: int | None) -> Decimal | None:
    return None if p is None else q2(Decimal(p) / 100)


# --- rates ---------------------------------------------------------------------


@dataclass(frozen=True)
class Rate:
    key: str  # e.g. flight_economy
    rate: Decimal  # percent, e.g. Decimal("18")
    itc_eligible: bool
    effective_from: date


@lru_cache(maxsize=4)
def load_rates(path: Path = RATES_FILE) -> Versions:
    """Versions sorted oldest first: ((effective_from, {key: {rate, itc_eligible}}), …)."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    versions = [(date.fromisoformat(v["effective_from"]), v["rates"]) for v in data["versions"]]
    return tuple(sorted(versions, key=lambda v: v[0]))


def rate_key(category: str, *, cabin: str | None = None, per_night_tariff: Decimal | None = None) -> str:
    if category == "flight":
        return "flight_premium" if (cabin or "").strip().lower() in PREMIUM_CABINS else "flight_economy"
    if category == "hotel":
        if per_night_tariff is None:
            raise ValueError("hotel GST needs the per-night tariff")
        return "hotel_upto_7500_per_night" if Decimal(per_night_tariff) <= HOTEL_SLAB else "hotel_above_7500"
    if category == "service_fee":
        return "service_fee"
    raise ValueError(f"unknown GST category {category!r} (use {', '.join(CATEGORIES)})")


def pick_rate(
    category: str,
    booked_on: date,
    *,
    cabin: str | None = None,
    per_night_tariff: Decimal | None = None,
    versions: Versions | None = None,
) -> Rate:
    """The rate in force on `booked_on` for this category / cabin / hotel tariff."""
    key = rate_key(category, cabin=cabin, per_night_tariff=per_night_tariff)
    versions = versions if versions is not None else load_rates()
    live = [(eff, rates) for eff, rates in versions if eff <= booked_on]
    if not live:
        raise ValueError(f"no GST rates in force on {booked_on.isoformat()}")
    eff, rates = live[-1]
    r = rates[key]
    return Rate(key, Decimal(str(r["rate"])), bool(r.get("itc_eligible", False)), eff)


def taxable_from_inclusive(gross: Decimal, rate: Decimal) -> Decimal:
    """Back out the taxable value from a GST-inclusive amount."""
    return q2(Decimal(gross) * 100 / (100 + Decimal(rate)))


# --- computation -----------------------------------------------------------------


@dataclass(frozen=True)
class Split:
    cgst: Decimal
    sgst: Decimal
    igst: Decimal

    @property
    def total(self) -> Decimal:
        return self.cgst + self.sgst + self.igst


def split(taxable: Decimal, rate: Decimal, *, intra_state: bool) -> Split:
    """Intra-state: CGST + SGST, half the rate each (each rounded). Inter-state: IGST."""
    taxable, rate = Decimal(taxable), Decimal(rate)
    zero = Decimal("0.00")
    if intra_state:
        half = q2(taxable * rate / 200)
        return Split(half, half, zero)
    return Split(zero, zero, q2(taxable * rate / 100))


@dataclass(frozen=True)
class InvoiceGst:
    """GST heads as printed on the supplier's invoice (₹)."""

    cgst: Decimal = Decimal("0")
    sgst: Decimal = Decimal("0")
    igst: Decimal = Decimal("0")


@dataclass(frozen=True)
class GstResult:
    category: str
    taxable_value: Decimal
    gst_rate: Decimal
    cgst: Decimal
    sgst: Decimal
    igst: Decimal
    total_gst: Decimal  # the invoice's figure when given, else computed
    computed_gst: Decimal
    mismatch: bool  # invoice total GST differs from computed by more than ₹1
    supplier_state: str | None
    itc_eligible: bool
    itc_status: str  # na | pending ('claimed' is set later, by hand)


def compute(
    category: str,
    taxable_value: Decimal,
    booked_on: date,
    *,
    cabin: str | None = None,
    per_night_tariff: Decimal | None = None,
    supplier_gstin: str | None = None,
    supplier_state: str | None = None,
    company_gstin: str | None = None,
    company_state: str | None = None,
    invoice: InvoiceGst | None = None,
    versions: Versions | None = None,
) -> GstResult:
    """GST for one booking.

    State: explicit code, else the GSTIN's first two digits. Same state as the company → CGST+SGST,
    otherwise (including unknown) IGST. No company GSTIN → still computed, itc_status 'na'.
    With `invoice`, its heads are stored and compared with the computed total.
    """
    taxable = q2(taxable_value)
    r = pick_rate(category, booked_on, cabin=cabin, per_night_tariff=per_night_tariff, versions=versions)
    sup_state = ((supplier_state or "").strip() or state_of(supplier_gstin) or "").zfill(2) or None
    co_state = ((company_state or "").strip() or state_of(company_gstin) or "").zfill(2) or None
    sup_state = None if sup_state == "00" else sup_state
    co_state = None if co_state == "00" else co_state
    computed = split(taxable, r.rate, intra_state=bool(sup_state and sup_state == co_state))

    shown, mismatch = computed, False
    if invoice is not None:
        shown = Split(q2(invoice.cgst), q2(invoice.sgst), q2(invoice.igst))
        mismatch = abs(shown.total - computed.total) > MISMATCH_TOLERANCE

    return GstResult(
        category=category,
        taxable_value=taxable,
        gst_rate=r.rate,
        cgst=shown.cgst,
        sgst=shown.sgst,
        igst=shown.igst,
        total_gst=shown.total,
        computed_gst=computed.total,
        mismatch=mismatch,
        supplier_state=sup_state,
        itc_eligible=r.itc_eligible,
        itc_status="pending" if normalize(company_gstin) and r.itc_eligible else "na",
    )


def cabin_of(offer_raw: Any) -> str | None:
    """First `cabin_class` found in a supplier offer (LetsFG puts it on each segment)."""
    if isinstance(offer_raw, dict):
        v = offer_raw.get("cabin_class") or offer_raw.get("cabin")
        if isinstance(v, str) and v:
            return v
        offer_raw = list(offer_raw.values())
    if isinstance(offer_raw, list):
        for item in offer_raw:
            if (found := cabin_of(item)) is not None:
                return found
    return None


# --- periods (Slack `gst [month]`, dashboard filters) ------------------------------

_MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]


def month_range(year: int, month: int) -> tuple[date, date]:
    """[first day, first day of next month)."""
    return date(year, month, 1), date(year + month // 12, month % 12 + 1, 1)


def fy_range(fy_start_year: int) -> tuple[date, date]:
    """Indian FY: 1 Apr – 31 Mar. fy_range(2026) = FY 2026-27."""
    return date(fy_start_year, 4, 1), date(fy_start_year + 1, 4, 1)


def fy_of(d: date) -> int:
    return d.year if d.month >= 4 else d.year - 1


def _year(s: str) -> int:
    y = int(s)
    return y + 2000 if y < 100 else y


def parse_period(text: str | None, today: date) -> tuple[date, date, str]:
    """'' → this month; '2026-11', '11/2026', 'nov', 'Nov 2026' → that month; 'fy', 'FY2026-27',
    'fy26' → that financial year. A bare month name means its most recent occurrence.
    Returns (start, end_exclusive, label). Raises ValueError on anything else."""
    t = (text or "").strip().lower()
    if not t:
        s, e = month_range(today.year, today.month)
        return s, e, s.strftime("%b %Y")
    if t.startswith("fy"):
        rest = t[2:].strip(" -'")
        if not rest:
            y = fy_of(today)
        elif m := re.fullmatch(r"(\d{2}|\d{4})(?:\s*-\s*\d{2,4})?", rest):
            y = _year(m.group(1))
        else:
            raise ValueError(f"not a financial year: {text!r} (try FY2026-27)")
        s, e = fy_range(y)
        return s, e, f"FY {y}-{str(y + 1)[2:]}"
    if m := re.fullmatch(r"(\d{4})-(\d{1,2})", t):
        year, month = int(m.group(1)), int(m.group(2))
    elif m := re.fullmatch(r"(\d{1,2})/(\d{4})", t):
        year, month = int(m.group(2)), int(m.group(1))
    elif (m := re.fullmatch(r"([a-z]{3})[a-z]*\.?\s*'?(\d{2}|\d{4})?", t)) and m.group(1) in _MONTHS:
        month = _MONTHS.index(m.group(1)) + 1
        if m.group(2):
            year = _year(m.group(2))
        else:
            year = today.year if month <= today.month else today.year - 1
    else:
        raise ValueError(f"not a month: {text!r} (try 2026-11, Nov, or FY2026-27)")
    if not 1 <= month <= 12:
        raise ValueError(f"not a month: {text!r}")
    s, e = month_range(year, month)
    return s, e, s.strftime("%b %Y")
