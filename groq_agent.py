"""Groq provider.

Uses Groq's official SDK with function tool-calling for the SEARCH phase:
the model is given a small set of LetsFG tools (airport lookup + flight
search) and runs a tool loop against our MCP client.

For safety, the model is NEVER given booking tools: `book_flight` and
`get_flight_booking` are only ever called deterministically from Python after
the manager approves. The final flight pick is also deterministic in Python,
applying `BOOKING_RULE` and `MAX_PRICE`, so a sloppy model cannot overspend.

Same interface as claude_agent:
    pick(request, traveller) -> dict
    confirm(picked, traveller) -> dict
    booking_status(booking_ref) -> dict
    book(request, traveller) -> dict
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import date, datetime, timedelta
from typing import Any

import groq
from groq import Groq

import config
import db
import letsfg_booking
import mcp_client

log = logging.getLogger("groq_agent")

_PROVIDER = "groq"
_TIME_WINDOWS = {
    "early morning": (4, 8),
    "morning": (6, 12),
    "afternoon": (12, 17),
    "evening": (17, 21),
    "night": (21, 28),
    "late night": (22, 28),
    "any": (0, 24),
}

# Search-only tools. book_flight is intentionally not here.
_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "find_airports",
            "description": "Look up IATA airport codes for a city or airport name.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "City or airport name"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_flights",
            "description": "Start a flight search. Use IATA codes resolved from find_airports.",
            "parameters": {
                "type": "object",
                "properties": {
                    "origin": {"type": "string", "description": "IATA origin code"},
                    "destination": {"type": "string", "description": "IATA destination code"},
                    "depart_date": {"type": "string", "description": "YYYY-MM-DD"},
                    "return_date": {"type": "string", "description": "YYYY-MM-DD for round-trip, else omit"},
                    "pax": {"type": "integer", "default": 1},
                    "cabin": {"type": "string", "default": "economy"},
                },
                "required": ["origin", "destination", "depart_date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_flight_results",
            "description": "Poll for the ranked flight list for a previous search_id.",
            "parameters": {
                "type": "object",
                "properties": {
                    "search_id": {"type": "string"},
                    "wait_for_all": {"type": "boolean", "default": True},
                },
                "required": ["search_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": "Call when the search is complete. You must pass the search_id you used.",
            "parameters": {
                "type": "object",
                "properties": {
                    "search_id": {"type": "string"},
                    "time_window": {
                        "type": "string",
                        "enum": list(_TIME_WINDOWS.keys()),
                        "description": "Which time-of-day window the user asked for; 'any' if none.",
                    },
                },
                "required": ["search_id", "time_window"],
            },
        },
    },
]

_SYSTEM = """You are an automated flight-search assistant for a corporate travel bot.

Your only job:
1. Read the user's one-line request.
2. Call `find_airports` if the origin or destination is a city rather than an IATA code.
3. Call `search_flights` once with the resolved IATA codes, depart_date and any return_date.
4. Call `get_flight_results` (with wait_for_all=true) repeatedly until it returns a non-empty
   offers list or clearly says the search is complete.
5. When the search is done, call `finish` with the same `search_id` and the time_window the
   user asked for (one of: early morning, morning, afternoon, evening, night, late night, any).

Do NOT try to book anything. Do NOT invent airport codes. Interpret dates against the "today"
value the user gives you. Be terse; the orchestrator reads tool results, not your prose."""


# --- Groq transport ---------------------------------------------------------


_client_singleton: Groq | None = None


def _groq_client() -> Groq:
    global _client_singleton
    if _client_singleton is None:
        _client_singleton = Groq(api_key=config.GROQ_API_KEY)
    return _client_singleton


_mcp_singleton: mcp_client.MCPClient | None = None


def _mcp() -> mcp_client.MCPClient:
    global _mcp_singleton
    if _mcp_singleton is None:
        _mcp_singleton = mcp_client.MCPClient(config.LETSFG_MCP_URL, config.LETSFG_TOKEN)
    return _mcp_singleton


def _redact(value: Any) -> str:
    """Trim secrets out of anything we log."""
    s = json.dumps(value, ensure_ascii=False, default=str)
    for secret in (getattr(config, "GROQ_API_KEY", ""), getattr(config, "LETSFG_TOKEN", "")):
        if secret:
            s = s.replace(secret, "***")
    return s[:600]


# --- Tool loop --------------------------------------------------------------


# Tools the model is *ever* allowed to execute via MCP. Defence-in-depth:
# Groq's SDK already restricts tool_calls to names in the registered schema,
# but we re-check here so a hallucinated or exploited name (especially
# `book_flight` / `get_flight_booking`) is refused before any outbound call.
_ALLOWED_MCP_TOOLS = frozenset({"find_airports", "search_flights", "get_flight_results"})


def _run_tool_call(name: str, raw_args: str) -> tuple[dict, dict | None]:
    """Execute one tool call from the model.

    Returns (payload_to_show_model, extracted_structured_json_or_None).
    """
    if name not in _ALLOWED_MCP_TOOLS:
        log.warning("refused model tool call to disallowed tool %r", name)
        return {"error": f"tool {name!r} is not allowed from the model"}, None
    try:
        args = json.loads(raw_args) if raw_args else {}
    except json.JSONDecodeError as e:
        return {"error": f"malformed arguments JSON: {e}"}, None
    if not isinstance(args, dict):
        return {"error": "arguments must be a JSON object"}, None

    try:
        result = _mcp().call_tool(name, args)
    except mcp_client.MCPError as e:
        return {"error": str(e)[:400]}, None
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"[:400]}, None

    data = mcp_client.tool_result_json(result)
    text_fallback = mcp_client.tool_result_text(result)
    payload = data if isinstance(data, (dict, list)) else {"text": text_fallback}
    # Give the model a compact view only.
    compact = _compact_tool_payload(name, payload)
    return compact, data if isinstance(data, dict) else None


def _compact_tool_payload(name: str, payload: Any) -> dict:
    """Trim long tool results down to what the model actually needs to decide."""
    if name == "find_airports":
        items = payload if isinstance(payload, list) else (payload.get("airports") or payload.get("results") or [])
        trimmed = []
        for a in items[:5] if isinstance(items, list) else []:
            trimmed.append({
                "iata": a.get("iata") or a.get("code") or a.get("airport_code"),
                "name": a.get("name") or a.get("airport_name"),
                "city": a.get("city"),
            })
        return {"airports": trimmed}
    if name == "search_flights":
        if isinstance(payload, dict):
            return {"search_id": payload.get("search_id") or payload.get("id"),
                    "complete": bool(payload.get("complete"))}
        return {"raw": str(payload)[:400]}
    if name == "get_flight_results":
        if isinstance(payload, dict):
            offers = payload.get("offers") or payload.get("results") or []
            return {
                "search_id": payload.get("search_id"),
                "complete": bool(payload.get("complete", True)),
                "offer_count": len(offers) if isinstance(offers, list) else 0,
            }
        return {"raw": str(payload)[:400]}
    return {"raw": str(payload)[:400]}


def _call_groq_with_retry(messages: list, max_attempts: int = 4) -> Any:
    """Call Groq with exponential backoff for 429 / transient 5xx."""
    delay = 1.0
    for attempt in range(1, max_attempts + 1):
        try:
            return _groq_client().chat.completions.create(
                model=config.GROQ_MODEL,
                messages=messages,
                tools=_TOOLS,
                tool_choice="auto",
                temperature=0,
                timeout=60,
            )
        except groq.RateLimitError as e:
            if attempt == max_attempts:
                raise
            log.warning("Groq rate limited (attempt %d): %s", attempt, e)
            time.sleep(delay)
            delay = min(delay * 2, 20)
        except groq.APIConnectionError as e:
            if attempt == max_attempts:
                raise
            log.warning("Groq connection error (attempt %d): %s", attempt, e)
            time.sleep(delay)
            delay = min(delay * 2, 20)


def _drive_search(request: str) -> dict:
    """Run the Groq tool-call loop and collect search_id + offers.

    The model is only allowed to call search tools; on `finish` we stop and
    pull the full offer list ourselves (fresh, directly from MCP).
    """
    today = date.today().isoformat()
    user_msg = f'today: {today}\nrequest: "{request}"'
    messages: list = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": user_msg},
    ]
    captured: dict = {"search_id": None, "time_window": "any"}

    for _round in range(10):
        resp = _call_groq_with_retry(messages)
        choice = resp.choices[0]
        msg = choice.message
        # record assistant turn as returned so tool_call ids match
        asst_payload: dict = {"role": "assistant", "content": msg.content or ""}
        if msg.tool_calls:
            asst_payload["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in msg.tool_calls
            ]
        messages.append(asst_payload)

        if not msg.tool_calls:
            # No tools and no finish — treat as giving up.
            raise ValueError(f"Groq ended without calling finish: {(msg.content or '')[:200]}")

        done = False
        for tc in msg.tool_calls:
            name = tc.function.name
            if name == "finish":
                try:
                    fin = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    fin = {}
                if fin.get("search_id"):
                    captured["search_id"] = fin["search_id"]
                captured["time_window"] = (fin.get("time_window") or "any").lower()
                if captured["time_window"] not in _TIME_WINDOWS:
                    captured["time_window"] = "any"
                messages.append({
                    "role": "tool", "tool_call_id": tc.id,
                    "content": json.dumps({"ok": True}),
                })
                done = True
                continue
            compact, structured = _run_tool_call(name, tc.function.arguments or "{}")
            if name == "search_flights" and structured:
                sid = structured.get("search_id") or structured.get("id")
                if sid:
                    captured["search_id"] = sid
            messages.append({
                "role": "tool", "tool_call_id": tc.id,
                "content": json.dumps(compact)[:4000],
            })

        if done:
            break
    else:
        raise ValueError("Groq tool loop ran out of rounds without finishing.")

    if not captured["search_id"]:
        raise ValueError("Groq never produced a search_id")

    # Pull the full ranked list ourselves — don't trust what we showed the model.
    offers = _full_offers(captured["search_id"])
    return {"search_id": captured["search_id"], "offers": offers,
            "time_window": captured["time_window"]}


def _full_offers(search_id: str, tries: int = 8) -> list[dict]:
    for i in range(tries):
        result = _mcp().call_tool(
            "get_flight_results", {"search_id": search_id, "wait_for_all": True}
        )
        data = mcp_client.tool_result_json(result) or {}
        offers = data.get("offers") or data.get("results") or []
        if offers:
            return offers
        complete = data.get("complete") or data.get("done")
        if complete:
            return offers
        time.sleep(2 + i)
    return []


# --- Deterministic selection (same rules as before) -------------------------


def _price(o: dict) -> float:
    for k in ("total_price", "price", "total", "amount"):
        v = o.get(k)
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, dict):
            a = v.get("amount") or v.get("total")
            if isinstance(a, (int, float)):
                return float(a)
    return float("inf")


def _bookable(o: dict) -> bool:
    if o.get("bookable") is False:
        return False
    if str(o.get("bookability", "")).lower() == "not_bookable_through_letsfg":
        return False
    return True


def _stops(o: dict) -> int:
    v = o.get("stops")
    if isinstance(v, int):
        return v
    segs = o.get("segments") or []
    return max(0, len(segs) - 1)


def _hour_of(value: Any) -> int | None:
    if not value or not isinstance(value, str):
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%H:%M"):
        try:
            return datetime.strptime(value[:19], fmt).hour
        except ValueError:
            continue
    m = re.search(r"T?(\d{1,2}):(\d{2})", value)
    return int(m.group(1)) if m else None


def _in_window(offer: dict, window: str) -> bool:
    if window == "any":
        return True
    lo, hi = _TIME_WINDOWS[window]
    dep = offer.get("depart_time") or offer.get("departure_time") or offer.get("depart")
    hour = _hour_of(dep)
    if hour is None:
        return True
    if window in ("night", "late night"):
        return hour >= lo % 24 or hour < hi % 24
    return lo <= hour < hi


def _choose(offers: list[dict], window: str) -> dict | None:
    pool = [o for o in offers if _bookable(o) and _in_window(o, window)]
    if not pool:
        return None
    nonstops = [o for o in pool if _stops(o) == 0]
    pool = nonstops or pool
    if config.MAX_PRICE:
        pool = [o for o in pool if _price(o) <= config.MAX_PRICE]
        if not pool:
            return None
    pool.sort(key=_price)
    return pool[0]


def _summary(offer: dict) -> str:
    carrier = offer.get("carrier") or offer.get("airline") or offer.get("marketing_carrier") or ""
    flight = offer.get("flight_number") or offer.get("number") or ""
    origin = offer.get("origin") or offer.get("from") or ""
    destination = offer.get("destination") or offer.get("to") or ""
    dep = offer.get("depart_time") or offer.get("departure_time") or ""
    arr = offer.get("arrive_time") or offer.get("arrival_time") or ""
    stops = offer.get("stops")
    stopstr = "nonstop" if stops == 0 else (f"{stops} stop" if stops else "")
    bits = [p for p in [f"{carrier} {flight}".strip(), dep, f"{origin} → {destination}", arr, stopstr] if p]
    return " · ".join(bits)


def _price_and_currency(offer: dict) -> tuple[float | None, str]:
    for k in ("total_price", "price", "total", "amount"):
        v = offer.get(k)
        if isinstance(v, (int, float)):
            return float(v), offer.get("currency") or config.CURRENCY
        if isinstance(v, dict):
            amt = v.get("amount") or v.get("total")
            cur = v.get("currency") or config.CURRENCY
            if isinstance(amt, (int, float)):
                return float(amt), cur
    return None, config.CURRENCY


# --- Public interface -------------------------------------------------------


def pick(request: str, traveller: dict) -> dict:
    try:
        search = _drive_search(request)
    except mcp_client.MCPError as e:
        return {"status": "failed", "message": f"LetsFG error: {e}", "provider": _PROVIDER}
    except groq.AuthenticationError as e:
        return {"status": "failed", "message": f"Groq auth failed: {e}", "provider": _PROVIDER}
    except groq.RateLimitError as e:
        return {"status": "failed", "message": f"Groq rate-limited: {e}", "provider": _PROVIDER}
    except Exception as e:  # noqa: BLE001
        log.exception("groq pick failed")
        return {"status": "need_info", "message": str(e)[:300], "provider": _PROVIDER}

    offer = _choose(search["offers"], search["time_window"])
    if offer is None:
        return {
            "status": "no_match",
            "message": "No flight matched the rule within MAX_PRICE in the requested time window.",
            "provider": _PROVIDER,
        }
    price, cur = _price_and_currency(offer)
    return {
        "status": "picked",
        "summary": _summary(offer),
        "price": price,
        "currency": cur,
        "search_id": search["search_id"],
        "offer_id": offer.get("offer_id") or offer.get("id"),
        "offer_link": offer.get("link") or offer.get("url"),
        "offer": offer,
        "time_window": search["time_window"],
        "provider": _PROVIDER,
        "backend": "mcp",
    }


def confirm(picked: dict, traveller: dict, booking_id: int | None = None) -> dict:
    """Book the already-picked offer. Deterministic MCP call via the shared
    `letsfg_booking` helper, with an idempotency guard against double-booking."""
    if config.DRY_RUN:
        return {**picked, "status": "picked",
                "message": "DRY_RUN is on: not booking.", "provider": _PROVIDER}
    search_id = picked.get("search_id")
    offer_id = picked.get("offer_id")
    if not (search_id and offer_id):
        return {"status": "failed",
                "message": "Missing search_id/offer_id to book.",
                "provider": _PROVIDER}
    err = letsfg_booking.validate_traveller(traveller)
    if err:
        return {**picked, "status": "need_info",
                "message": err, "provider": _PROVIDER}
    if booking_id is not None:
        existing = db.get(booking_id) or {}
        if existing.get("booking_ref"):
            return {**picked,
                    "status": existing.get("status") or "awaiting_approval",
                    "booking_ref": existing["booking_ref"],
                    "message": "booking already issued; skipping duplicate book_flight call",
                    "provider": _PROVIDER}
        if not db.claim_booking_start(booking_id):
            latest = db.get(booking_id) or {}
            return {**picked,
                    "status": latest.get("status") or "failed",
                    "booking_ref": latest.get("booking_ref"),
                    "message": "another handler is already booking this row",
                    "provider": _PROVIDER}
    out = letsfg_booking.book(picked, traveller, booking_id)
    return {**picked, **out, "provider": _PROVIDER}


def booking_status(booking_ref: str, booking_id: int | None = None) -> dict:
    out = letsfg_booking.status(booking_ref, booking_id)
    out["provider"] = _PROVIDER
    return out


def book(request: str, traveller: dict, booking_id: int | None = None) -> dict:
    picked = pick(request, traveller)
    if picked.get("status") != "picked":
        return picked
    return confirm(picked, traveller, booking_id=booking_id)
