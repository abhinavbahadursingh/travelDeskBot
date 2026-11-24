"""Money (integer paise) and time (UTC stored, IST shown) helpers.

No floats touch money after the supplier boundary: amounts arrive from LetsFG as
float rupees (or strings) and are converted once, exactly, with Decimal.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


# --- money -----------------------------------------------------------------


def to_paise(rupees: float | int | str | Decimal) -> int:
    """₹ amount → integer paise, rounded half-up at the paisa. Raises ValueError on junk."""
    try:
        d = Decimal(str(rupees))
    except (InvalidOperation, ValueError) as e:
        raise ValueError(f"not a money amount: {rupees!r}") from e
    if not d.is_finite():
        raise ValueError(f"not a money amount: {rupees!r}")
    return int((d * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def format_inr(paise: int, *, show_paise: bool = False) -> str:
    """12345678 → '₹1,23,456' (Indian digit grouping). Negative → '-₹…'."""
    sign = "-" if paise < 0 else ""
    rupees, p = divmod(abs(int(paise)), 100)
    s = str(rupees)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        s = ",".join(groups) + "," + tail
    out = f"{sign}₹{s}"
    if show_paise or p:
        out += f".{p:02d}"
    return out


# --- time ------------------------------------------------------------------


def utcnow() -> datetime:
    return datetime.now(UTC)


def as_utc(dt: datetime, *, naive_tz: ZoneInfo = IST) -> datetime:
    """Aware → UTC. Naive → interpreted in `naive_tz` (supplier local times are IST for domestic)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=naive_tz)
    return dt.astimezone(UTC)


def parse_local(value: str | None, *, tz: ZoneInfo = IST) -> datetime | None:
    """'2026-11-22T06:05' (supplier local time) → aware UTC datetime. None/junk → None."""
    if not value:
        return None
    try:
        return as_utc(datetime.fromisoformat(str(value)), naive_tz=tz)
    except ValueError:
        return None


def to_ist(dt: datetime | None) -> datetime | None:
    return as_utc(dt, naive_tz=ZoneInfo("UTC")).astimezone(IST) if dt else None


def fmt_ist(dt: datetime | None, pattern: str = "%d %b %H:%M") -> str:
    """UTC datetime → '22 Nov 06:05' in IST, for Slack text."""
    local = to_ist(dt)
    return local.strftime(pattern) if local else "—"
