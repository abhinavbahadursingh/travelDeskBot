"""LetsFG offers → the /book page's picks, and a chosen offer → a pick record.

Pure functions: no I/O, no Slack, no DB.
"""

from __future__ import annotations

from datetime import datetime

from traveldesk import config
from traveldesk.booking import flight_scope

# (tag, tone) per pick slot, in the order the page shows them.
BEST_VALUE = ("Best value", "blue")
CHEAPEST = ("Cheapest", "green")
FASTEST = ("Fastest", "green")
FLEXIBLE = ("Most flexible", "violet")
NEXT_CHEAPEST = ("Also good", "violet")

# A "best value" flight may take this much longer than the quickest one.
_VALUE_DURATION_SLACK = 1.25


def price(offer: dict) -> float | None:
    return flight_scope.offer_price(offer)


def _conditions(offer: dict) -> dict:
    c = offer.get("conditions")
    return c if isinstance(c, dict) else {}


def _outbound_segments(offer: dict) -> list[dict]:
    ob = offer.get("outbound")
    segs = ob.get("segments") if isinstance(ob, dict) else None
    return [s for s in segs or [] if isinstance(s, dict)] or flight_scope.segments(offer)


def stops(offer: dict) -> int:
    return max(len(_outbound_segments(offer)) - 1, 0)


def _time(value) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value)[:19])
    except ValueError:
        return None


def duration_seconds(offer: dict) -> int | None:
    ob = offer.get("outbound")
    total = ob.get("total_duration_seconds") if isinstance(ob, dict) else None
    if isinstance(total, (int, float)) and total > 0:
        return int(total)
    segs = _outbound_segments(offer)
    start, end = (_time(segs[0].get("departure")), _time(segs[-1].get("arrival"))) if segs else (None, None)
    return int((end - start).total_seconds()) if start and end and end > start else None


def _allowance(value) -> int:
    """LetsFG refund/change rule → 0 not allowed or unknown, 1 with a fee, 2 free."""
    v = str(value or "").lower()
    if not v or "not" in v:
        return 0
    if "fee" in v:
        return 1
    return 2 if ("allowed" in v or "free" in v) else 0


def flexibility(offer: dict) -> int:
    c = _conditions(offer)
    return 2 * _allowance(c.get("refund_before_departure")) + _allowance(c.get("change_before_departure"))


def rank(offers: list[dict]) -> list[tuple[tuple[str, str], dict]]:
    """Up to three distinct picks: best value, cheapest (or fastest), most flexible.

    Best value = the cheapest of the flights with the fewest stops that take at
    most 25% longer than the quickest of them. Deterministic, so the same search
    always shows the same picks."""
    priced = sorted((o for o in offers if price(o) is not None), key=lambda o: (price(o), str(o.get("id"))))
    if not priced:
        return []
    fewest = min(stops(o) for o in priced)
    direct = [o for o in priced if stops(o) == fewest]
    quickest = min((d for o in direct if (d := duration_seconds(o))), default=None)
    pool = [
        o
        for o in direct
        if quickest is None or (duration_seconds(o) or 0) <= quickest * _VALUE_DURATION_SLACK
    ]
    picks = [(BEST_VALUE, (pool or direct)[0])]
    used = {id(picks[0][1])}

    def take(tag, candidates):
        for o in candidates:
            if id(o) not in used:
                picks.append((tag, o))
                used.add(id(o))
                return True
        return False

    by_duration = sorted(priced, key=lambda o: (duration_seconds(o) or 10**9, price(o)))
    if id(priced[0]) not in used:
        take(CHEAPEST, priced)
    else:
        take(FASTEST, by_duration)
    flexible = sorted((o for o in priced if flexibility(o) > 0), key=lambda o: (-flexibility(o), price(o)))
    if not take(FLEXIBLE, flexible) and not take(FASTEST, by_duration):
        take(NEXT_CHEAPEST, priced)
    return picks


# --- card text -------------------------------------------------------------------------


def _hhmm(value) -> str:
    t = _time(value)
    return t.strftime("%H:%M") if t else "—"


def _duration_text(seconds: int | None) -> str:
    if not seconds:
        return "—"
    h, m = divmod(round(seconds / 60), 60)
    return f"{h}h {m:02d}m"


def _bags(offer: dict) -> str:
    c = _conditions(offer)
    checked, cabin = str(c.get("checked_bag") or "").lower(), str(c.get("carry_on") or "").lower()
    if "included" in checked:
        return "Checked bag + cabin bag" if "included" in cabin else "Checked bag included"
    if "unknown" in checked:
        return "Checked bag not confirmed"
    if "included" in cabin:
        return "Cabin bag only"
    return "Baggage not listed"


def _fare_rules(offer: dict) -> str:
    c = _conditions(offer)
    refund, change = (
        _allowance(c.get("refund_before_departure")),
        _allowance(c.get("change_before_departure")),
    )
    if refund == 2:
        return "Free cancellation"
    if refund == 1:
        return "Refundable with a fee"
    if change:
        return "Free changes" if change == 2 else "Changeable with a fee"
    if c.get("refund_before_departure") or c.get("change_before_departure"):
        return "Non-refundable"
    return "Fare rules not listed"


def card(offer: dict, tag: tuple[str, str] | None = None) -> dict:
    """What one option card on the page needs. No booking ids beyond the offer id."""
    segs = _outbound_segments(offer)
    first, last = (segs[0], segs[-1]) if segs else ({}, {})
    details = flight_scope.offer_details(offer)
    dep, arr = _time(first.get("departure")), _time(last.get("arrival"))
    n = stops(offer)
    cabin = str(first.get("cabin_class") or "").lower()
    return {
        "id": str(offer.get("id")),
        "tag": tag[0] if tag else None,
        "tone": tag[1] if tag else None,
        "airline": f"{details['airline_name'] or 'Flight'} · "
        + ("Nonstop" if n == 0 else f"{n} stop" + ("s" if n > 1 else "")),
        "airline_name": details["airline_name"] or "Flight",
        "airline_code": str(first.get("airline") or offer.get("owner_airline") or "")[:3] or None,
        "stops": n,
        "stop_airports": [str(s.get("destination") or "") for s in segs[:-1] if s.get("destination")],
        "duration_seconds": duration_seconds(offer),
        "origin": str(first.get("origin") or ""),
        "destination": str(last.get("destination") or ""),
        "depart_at": details["depart_at"],
        "dep": _hhmm(first.get("departure")),
        "arr": _hhmm(last.get("arrival")),
        "arr_day_offset": (arr.date() - dep.date()).days if dep and arr else 0,
        "duration": _duration_text(duration_seconds(offer)),
        "bags": _bags(offer),
        "fare_rules": _fare_rules(offer),
        "fare_conditions": details["fare_conditions"],
        "fare_family": _conditions(offer).get("fare_family"),
        "seller": _conditions(offer).get("seller_label"),
        "cabin": cabin or None,
        "price": price(offer),
        "currency": offer.get("currency") or config.CURRENCY,
    }


# --- the record the Slack flow books from ------------------------------------------------


def pick_record(route: dict, search_id: str, offer: dict, *, offers_found: int) -> dict:
    """The same shape agent.pick returns for a Slack request, so confirm, booking,
    re-pricing and alternatives work on it unchanged."""
    dep, ret = route["date_from"], route["return_from"]
    details = flight_scope.offer_details(offer)
    return {
        "provider": config.AI_PROVIDER,
        "backend": "rest",
        "origin": route["origin"],
        "destination": route["destination"],
        "depart_date": dep,
        "return_from": ret,
        "passengers_requested": route["passengers"],
        "offers_found": offers_found,
        "status": "picked",
        "model": "web",  # chosen by the traveller on the web page, not by Claude
        "summary": details["auto_summary"],
        "price": price(offer),
        "currency": offer.get("currency") or config.CURRENCY,
        "search_id": search_id,
        "offer_id": offer["id"],
        **details,
        "last_flight_date": flight_scope.last_flight_date(offer) or ret or dep,
        "offer_link": offer.get("booking_url"),
        "message": "Chosen on the travel desk web page",
    }
