"""Is a LetsFG offer a domestic-India trip? When is its last flight?

LetsFG's offer segments carry only IATA codes and city names, and its
find_airports tool returns an empty "country" (checked 2026-11-18). So the
country of each airport is taken from, in order:

  1. a country field on the segment, if LetsFG ever sends one;
  2. INDIA_AIRPORTS below — Indian airports with scheduled passenger service.

Anything else is "unknown", and unknown means international (the safe
default: the traveller is asked for passport details).
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime

# Indian airports with scheduled commercial service. Missing codes only make a
# trip look international (passport asked), never the other way round.
INDIA_AIRPORTS = frozenset(
    """
DEL HDO DXN BOM NMI BLR MAA CCU HYD COK TRV CCJ CNN IXE TRZ IXM CJB TCR SXV PNY
AMD BDQ STV RAJ HSR BHU JGA PBD BHJ IXK DIU IXY PNQ NAG KLH ISK IXU NDC SAG JLG SSE
GOI GOX JAI UDR JDH BKB KQH ATQ IXC LUH IXP AIP BUP IXJ SXR IXL KUU DHM SLV DED PGH
LKO VNS GOP IXD KNU AGR AYJ BEK HSS IDR BHO JLR GWL HJR REW RPR JRG PAB JGB
BBI IXR PAT GAY DBR IXB DGH RDP GAU IXA IMF AJL DMU IXS JRH DIB TEZ IXI HGI PYG SHL
IXZ AGX VGA VTZ RJA TIR CDP KJB HBX IXG MYQ GBI VDY RQY PUT
""".split()
)

_COUNTRY_KEYS = {
    "origin": ("origin_country", "origin_country_code", "departure_country"),
    "destination": ("destination_country", "destination_country_code", "arrival_country"),
}


def segments(offer: dict) -> list[dict]:
    segs: list[dict] = []
    for leg in ("outbound", "inbound"):
        part = offer.get(leg)
        if isinstance(part, dict):
            segs += [s for s in part.get("segments") or [] if isinstance(s, dict)]
    # Some shapes put segments at the top level.
    segs += [s for s in offer.get("segments") or [] if isinstance(s, dict)]
    return segs


def airport_country(segment: dict, side: str) -> str | None:
    """2-letter country of the segment's origin/destination, or None if unknown."""
    for key in _COUNTRY_KEYS[side]:
        v = segment.get(key)
        if isinstance(v, str) and len(v.strip()) == 2:
            return v.strip().upper()
    code = str(segment.get(side) or "").strip().upper()
    if code in INDIA_AIRPORTS:
        return "IN"
    return None


def is_domestic_india(offer: dict | None) -> bool:
    """True only if every segment starts and ends at an airport known to be in India."""
    if not isinstance(offer, dict):
        return False
    segs = segments(offer)
    if not segs:
        return False
    return all(airport_country(s, side) == "IN" for s in segs for side in ("origin", "destination"))


def airports(offer: dict | None) -> list[str]:
    out: list[str] = []
    for s in segments(offer or {}):
        for side in ("origin", "destination"):
            code = str(s.get(side) or "").upper()
            if code and (not out or out[-1] != code):
                out.append(code)
    return out


def _dates(segs: Iterable[dict]) -> list[date]:
    out = []
    for s in segs:
        for key in ("arrival", "departure"):
            raw = str(s.get(key) or "")[:10]
            try:
                out.append(date.fromisoformat(raw))
            except ValueError:
                pass
    return out


def last_flight_date(offer: dict | None) -> str | None:
    """ISO date of the trip's last flight (return leg included), if known."""
    ds = _dates(segments(offer or {}))
    return max(ds).isoformat() if ds else None


def offer_price(offer: dict) -> float | None:
    for key in ("price_total", "price"):
        try:
            return float(offer[key])
        except (KeyError, TypeError, ValueError):
            continue
    return None


def signature(offer: dict | None) -> tuple:
    """Identifies the same flights across two searches (offer ids differ)."""
    return tuple(
        (
            str(s.get("airline") or ""),
            str(s.get("flight_no") or ""),
            str(s.get("origin") or ""),
            str(s.get("destination") or ""),
            str(s.get("departure") or "")[:16],
        )
        for s in segments(offer or {})
    )


def depart_at(offer: dict | None) -> str | None:
    segs = segments(offer or {})
    return str(segs[0].get("departure") or "")[:16] or None if segs else None


_RULE_TEXT = {
    "allowed": "Allowed, no fee",
    "allowed_with_fee": "Allowed with a fee",
    "not_allowed": "Not allowed",
}
NOT_GIVEN = "Not provided by the seller. Ask the airline before booking"


def _amount(value, currency) -> str | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return f"{v:,.0f} {currency}".strip() if v > 0 else None


def _rule(conditions: dict, key: str, penalty_keys: tuple, currency: str) -> str:
    raw = str(conditions.get(key) or "").strip().lower()
    if not raw or raw == "unknown":
        return NOT_GIVEN
    text = _RULE_TEXT.get(raw, raw.replace("_", " ").capitalize())
    if raw == "allowed_with_fee":
        fee = next((a for k in penalty_keys if (a := _amount(conditions.get(k), currency))), None)
        text += f" ({fee})" if fee else " (amount not given by the seller)"
    return text


def _bag(value) -> str | None:
    v = str(value or "").strip()
    return v[0].upper() + v[1:] if v else None


def fare_conditions(offer: dict) -> list[dict]:
    """The fare's rules as LetsFG gives them, one {label, value} row each, for
    the traveller to read before Confirm. Rules the seller didn't send are shown
    as not provided, never guessed. LetsFG sends no no-show rule today."""
    c = offer.get("conditions") if isinstance(offer.get("conditions"), dict) else {}
    cur = str(offer.get("currency") or "")
    rows = []

    def add(label, value):
        if value:
            rows.append({"label": label, "value": str(value)})

    add("Fare", c.get("fare_family"))
    add(
        "Cancel & refund before departure",
        _rule(c, "refund_before_departure", ("refund_penalty_amount",), cur),
    )
    add(
        "Change before departure",
        _rule(c, "change_before_departure", ("change_penalty_amount", "change_fee_amount"), cur),
    )
    no_show = c.get("no_show") or c.get("no_show_policy")
    add("No-show", _rule(c, "no_show", (), cur) if no_show else NOT_GIVEN)
    add("Checked bag", _bag(c.get("checked_bag")) or NOT_GIVEN)
    add("Cabin bag", _bag(c.get("carry_on")))
    add("Personal item", _bag(c.get("personal_item")))
    add("Seat selection", _bag(c.get("seat")))
    if str(c.get("self_transfer") or "").lower() == "self_transfer":
        add(
            "Self-transfer",
            "Separate tickets: you collect bags and check in again at the connection, "
            "and a missed connection is not covered",
        )
    add("Sold by", c.get("seller_label"))
    return rows


def offer_details(offer: dict) -> dict:
    """Facts about an offer that the booking flow keeps in the pick record."""
    segs = segments(offer)
    out_segs = (
        [s for s in ((offer.get("outbound") or {}).get("segments") or []) if isinstance(s, dict)]
        if isinstance(offer.get("outbound"), dict)
        else segs
    )
    first = segs[0] if segs else {}
    airline = offer.get("owner_airline") or (offer.get("airlines") or [None])[0] or first.get("airline")
    airline_name = first.get("airline_name") or airline
    fare_type = (
        (offer.get("conditions") or {}).get("fare_type")
        if isinstance(offer.get("conditions"), dict)
        else None
    )
    dep = depart_at(offer)
    route = airports(offer)
    stops = max(len(out_segs) - 1, 0)
    when = ""
    if dep:
        try:
            when = datetime.fromisoformat(dep).strftime("%d %b %H:%M") + " "
        except ValueError:
            when = ""
    summary = (
        f"{airline_name or 'Flight'} · {when}{' → '.join(route)} · "
        f"{'nonstop' if stops == 0 else f'{stops} stop' + ('s' if stops > 1 else '')}"
    )
    return {
        "domestic": is_domestic_india(offer),
        "airports": route,
        "last_flight_date": last_flight_date(offer),
        "depart_at": dep,
        "airline": airline,
        "airline_name": airline_name,
        "fare_type": fare_type,
        "fare_conditions": fare_conditions(offer),
        "signature": [list(s) for s in signature(offer)],
        "auto_summary": summary,
    }
