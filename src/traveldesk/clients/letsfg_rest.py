"""LetsFG Developer REST API client (X-API-Key auth).

Base: config.LETSFG_API_BASE (https://letsfg.co/developers/api/v1)
  POST /flights/search
  POST /flights/book
  GET  /flights/bookings/{booking_id}

/agents/register and /agents/connect-payment are deliberately not called:
both open or fund a billing account and must be done by a human.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

import httpx

from traveldesk import config

log = logging.getLogger("letsfg_rest")

TERMINAL_STATES = ("completed", "failed", "needs_attention")
_RETRYABLE_HTTP = (429, 502, 503, 504)

_http_singleton: httpx.Client | None = None


def _http() -> httpx.Client:
    global _http_singleton
    if _http_singleton is None:
        _http_singleton = httpx.Client(
            base_url=config.LETSFG_API_BASE,
            headers={
                "X-API-Key": config.LETSFG_API_KEY,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            timeout=120.0,
        )
    return _http_singleton


class RestError(RuntimeError):
    def __init__(self, status_code: int, body: dict):
        self.status_code = status_code
        self.body = body if isinstance(body, dict) else {}
        code = self.body.get("error") or self.body.get("detail") or ""
        super().__init__(f"LetsFG HTTP {status_code} {code}".strip())

    @property
    def code(self) -> str:
        return str(self.body.get("error") or self.body.get("detail") or "")

    @property
    def text(self) -> str:
        return str(self.body.get("message") or self.body.get("detail") or self.body.get("error") or "")


def _request(method: str, path: str, *, json_body: dict | None = None, retries: int = 0) -> dict:
    """One API call. Retries only transport errors and 429/5xx, and only when
    the caller says it is safe (book is safe because of idempotency_key)."""
    delay = 2.0
    for attempt in range(retries + 1):
        try:
            resp = _http().request(method, path, json=json_body)
        except httpx.HTTPError as e:
            if attempt < retries:
                log.warning("LetsFG %s %s transport error, retrying: %s", method, path, type(e).__name__)
                time.sleep(delay)
                delay *= 2
                continue
            raise RestError(0, {"error": "transport_error", "message": type(e).__name__}) from e
        if resp.status_code in _RETRYABLE_HTTP and attempt < retries:
            log.warning("LetsFG %s %s HTTP %s, retrying", method, path, resp.status_code)
            time.sleep(delay)
            delay *= 2
            continue
        try:
            body = resp.json() if resp.content else {}
        except ValueError:
            body = {"message": resp.text[:300]}
        if resp.status_code >= 400:
            raise RestError(resp.status_code, body)
        return body if isinstance(body, dict) else {"data": body}
    raise RestError(0, {"error": "retries_exhausted"})


# --- Search ----------------------------------------------------------------


def search_flights(
    origin: str,
    destination: str,
    date_from: str,
    *,
    return_from: str | None = None,
    adults: int = 1,
    cabin_class: str = "M",
    limit: int = 30,
) -> dict:
    """Returns {"status": "ok", "search_id", "offers", "currency"} or an error dict."""
    body: dict[str, Any] = {
        "origin": origin,
        "destination": destination,
        "date_from": date_from,
        "adults": adults,
        "cabin_class": cabin_class,
        "currency": config.CURRENCY,
        "limit": limit,
    }
    if return_from:
        body["return_from"] = return_from
    try:
        data = _request("POST", "/flights/search", json_body=body, retries=1)
    except RestError as e:
        return _error_result(e, phase="search")
    search_id = data.get("search_id") or data.get("id")
    offers = data.get("offers") or []
    if not search_id:
        return {
            "status": "failed",
            "message": "LetsFG search response had no search_id; cannot book from it.",
        }
    return {
        "status": "ok",
        "search_id": search_id,
        "offers": offers,
        "currency": data.get("currency") or config.CURRENCY,
    }


# --- Book ------------------------------------------------------------------

# F / M / X as on the passport (ICAO Doc 9303; IATA API/PADIS uses the same codes).
_GENDER = {"m": "m", "male": "m", "f": "f", "female": "f", "x": "x", "unspecified": "x"}
_OPTIONAL_PASSENGER_FIELDS = (
    "nationality",
    "phone_country",
    "passport_number",
    "passport_country",
    "passport_expiry",
    "passport_issue",
    "address_line1",
    "address_line2",
    "address_city",
    "address_state",
    "address_postal",
    "address_country",
)


def map_passenger(traveller: dict) -> dict:
    """Validated traveller profile → LetsFG REST passenger object."""
    # Airlines have no middle-name field: IATA names are SURNAME/GIVEN MIDDLE,
    # so middle names travel inside given_name.
    middle = str(traveller.get("middle_name") or "").strip()
    given = str(traveller["given_name"]).strip()
    p: dict[str, Any] = {
        "given_name": f"{given} {middle}" if middle else given,
        "family_name": str(traveller["family_name"]).strip(),
        "email": str(traveller["email"]).strip(),
        "passenger_type": "adult",
    }
    phone = traveller.get("phone_number") or traveller.get("phone")
    if phone:
        p["phone_number"] = str(phone).strip()
    born_on = traveller.get("born_on") or traveller.get("date_of_birth") or traveller.get("dob")
    if born_on:
        p["born_on"] = str(born_on).strip()
    gender = _GENDER.get(str(traveller.get("gender", "")).strip().lower())
    if gender:
        p["gender"] = gender
    for f in _OPTIONAL_PASSENGER_FIELDS:
        if traveller.get(f):
            p[f] = str(traveller[f]).strip()
    return p


def book_flight(search_id: str, offer_id: str, traveller: dict, idempotency_key: str) -> dict:
    body = {
        "search_id": search_id,
        "offer_id": offer_id,
        "passengers": [map_passenger(traveller)],
        "contact_email": str(traveller["email"]).strip(),
        "idempotency_key": idempotency_key[:64],
    }
    try:
        # Same idempotency_key on retry returns the same booking, so retrying
        # transient failures cannot double-charge.
        data = _request("POST", "/flights/book", json_body=body, retries=2)
    except RestError as e:
        out = _error_result(e, phase="book")
        # Exact error for logs, minus anything the server echoed back from
        # the passenger payload.
        out["error_detail"] = {
            "http_status": e.status_code,
            "error": e.code,
            "fields": _field_names(e.body.get("missing_fields")) + _field_names(e.body.get("invalid_fields")),
            "server_message": _scrub(e.text, body["passengers"][0])[:300],
            # Untruncated for the need_info diagnostic log; still redacted.
            "server_message_full": _scrub(e.text, body["passengers"][0]),
        }
        return out
    missing = parse_missing_fields(data)
    if not data.get("booking_id") and (
        missing or str(data.get("status") or data.get("state") or "").lower() == "need_info"
    ):
        # 200 with need_info: nothing was booked, LetsFG wants more fields.
        return {
            "status": "need_info",
            "missing_fields": missing,
            "message": "LetsFG needs more passenger details: "
            + (", ".join(missing) or "unspecified fields")
            + ".",
            "error_detail": {
                "http_status": 200,
                "error": "need_info",
                "fields": missing,
                "server_message": _scrub(str(data.get("message") or ""), body["passengers"][0])[:300],
                "server_message_full": _scrub(str(data.get("message") or ""), body["passengers"][0]),
            },
        }
    booking_id = data.get("booking_id")
    if not booking_id or data.get("ok") is False:
        return {"status": "failed", "message": data.get("message") or "LetsFG did not return a booking_id."}
    state = str(data.get("state") or "booking_in_progress").lower()
    if state == "awaiting_payment" and data.get("pay_url"):
        return payment_link(data, booking_id=booking_id, kind="payment_required")
    return {
        "status": _state_to_status(state, pnr=None),
        "booking_ref": booking_id,
        "approve_url": data.get("pay_url"),
        "message": data.get("message", ""),
    }


def payment_link(data: dict, *, booking_id=None, kind: str = "payment_declined") -> dict:
    """A LetsFG answer that says: pay on this secure page and the booking
    starts by itself (status "payment_link"). Works on the 402 book body,
    a failed status with `retry`, and an awaiting_payment status.

    `booking_ref` is the booking that paying starts — poll that one; never
    call /flights/book again for the request."""
    retry = data.get("retry") if isinstance(data.get("retry"), dict) else {}
    pay_url = retry.get("pay_url") or data.get("pay_url")
    new_ref = (
        retry.get("booking_id")
        or data.get("retry_booking_id")
        or _booking_id_from_poll_url(data.get("poll_url"))
        or booking_id
    )
    valid_until = retry.get("valid_until") or data.get("valid_until")
    try:
        valid_until = float(valid_until) if valid_until not in (None, "") else None
    except (TypeError, ValueError):
        valid_until = None
    return {
        "status": "payment_link",
        "payment_kind": kind,
        "booking_ref": new_ref,
        "declined_booking_ref": data.get("booking_id") if new_ref != data.get("booking_id") else None,
        "pay_url": pay_url,
        "pay_valid_until": valid_until,
        "pay_amount": retry.get("amount") or data.get("held_amount"),
        "pay_currency": retry.get("currency") or data.get("currency"),
        "decline_reason": data.get("decline_reason") or "",
        "decline_message": data.get("decline_message") or "",
        "decline_advice": data.get("decline_advice") or "",
        "message": _status_message(data, "failed"),
    }


def _booking_id_from_poll_url(url) -> str | None:
    if not url or "/flights/bookings/" not in str(url):
        return None
    return str(url).rstrip("/").rsplit("/", 1)[-1] or None


# --- Status ----------------------------------------------------------------


def get_booking(booking_id: str) -> dict:
    if not booking_id:
        return {"status": "needs_attention", "message": "no booking_id"}
    try:
        data = _request("GET", f"/flights/bookings/{booking_id}", retries=1)
    except RestError as e:
        if e.status_code in (0, 429, 502, 503, 504):
            # Transient: keep polling rather than declaring a final state.
            return {
                "status": "booking_in_progress",
                "transient": True,  # not a real state: callers must not act on it
                "message": f"status check temporarily failed ({e.status_code})",
            }
        return {"status": "needs_attention", "message": e.text or str(e)}
    state = str(data.get("state") or "").lower() or "booking_in_progress"
    pnr = data.get("pnr")
    retry = data.get("retry") if isinstance(data.get("retry"), dict) else {}
    if state == "failed" and (retry.get("pay_url") or data.get("pay_url")):
        # Declined hold with a 3-D Secure / pay-another-way page: the same
        # payment continues as a new booking (retry.booking_id).
        return {**payment_link(data, booking_id=booking_id), "state": state}
    status = _state_to_status(state, pnr=pnr)
    out = {
        "status": status,
        "state": state,
        "pnr": pnr if status == "completed" else None,
        "charged_amount": data.get("charged_amount") if status == "completed" else None,
        "currency": data.get("currency") or config.CURRENCY,
        "approve_url": data.get("pay_url"),
        "terminal": bool(data.get("terminal")),
        "failure_reason": data.get("failure_reason") or "",
        "decline_reason": data.get("decline_reason") or "",
        "message": _status_message(data, status),
    }
    out["total"] = out["charged_amount"]
    return out


def _state_to_status(state: str, *, pnr: str | None) -> str:
    if state == "completed":
        return "completed" if pnr else "needs_attention"
    if state == "awaiting_payment":
        return "awaiting_payment"  # a pay link the payer has not used yet
    if state in ("failed", "needs_attention"):
        return state
    return (
        "booking_in_progress" if state in ("", "authorised", "card_issued", "awaiting_settlement") else state
    )


def _status_message(data: dict, status: str) -> str:
    if status == "completed":
        return data.get("message", "")
    if status == "needs_attention" and str(data.get("state")) == "completed":
        return "LetsFG reported completed but returned no PNR — check with LetsFG."
    parts = [
        data.get("failure_reason"),
        data.get("decline_message"),
        data.get("decline_advice"),
        data.get("question"),
        data.get("message"),
    ]
    return " ".join(str(p) for p in parts if p)  # full text: nothing cut


# --- Errors ----------------------------------------------------------------


# LetsFG / common spellings → our profile field names.
_FIELD_ALIASES = {
    "born_on": "date_of_birth",
    "dob": "date_of_birth",
    "birth_date": "date_of_birth",
    "phone_number": "phone",
    "mobile": "phone",
    "passport_issue_date": "passport_issue",
    "passport_issued": "passport_issue",
    "passport_expiry_date": "passport_expiry",
    "passport_expires": "passport_expiry",
    "passport_issuing_country": "passport_country",
    "passport_country_code": "passport_country",
    "passport_no": "passport_number",
    "nationality_country": "nationality",
    "address_line_1": "address_line1",
    "address1": "address_line1",
    "street": "address_line1",
    "city": "address_city",
    "postal_code": "address_postal",
    "postcode": "address_postal",
    "zip": "address_postal",
    "address_postal_code": "address_postal",
    "country_of_residence": "address_country",
    "phone_country_code": "phone_country",
}
_MISSING_KEYS = (
    "missing_fields",
    "required_fields",
    "fields_required",
    "missing",
    "needed_fields",
    "fields_needed",
)


def _normalize_field(name) -> str:
    n = str(name).strip()
    # "passengers[0].passport_number" / "passengers.0.passport_number" → passport_number
    n = n.replace("]", "").split(".")[-1].split("[")[-1].lower()
    return _FIELD_ALIASES.get(n, n)


def parse_missing_fields(body) -> list:
    """Exactly which fields LetsFG says are missing, in order, de-duplicated."""
    if not isinstance(body, dict):
        return []
    raw = []
    for key in _MISSING_KEYS:
        raw += _field_names(body.get(key))
    if not raw and str(body.get("status") or body.get("error") or "").lower() == "need_info":
        raw += _field_names(body.get("fields"))
    for nested in ("detail", "error_details", "data"):
        if isinstance(body.get(nested), dict):
            raw += parse_missing_fields(body[nested])
    out = []
    for f in raw:
        n = _normalize_field(f)
        if n and n not in out and n != "none":
            out.append(n)
    return out


def _field_names(fields) -> list:
    if isinstance(fields, dict):
        return [str(f) for f in fields]
    if isinstance(fields, list):
        return [
            str((f.get("field") or f.get("name") or f.get("path")) if isinstance(f, dict) else f)
            for f in fields
        ]
    return [str(fields)] if fields else []


_EMAIL_RE = re.compile(r"[^\s@\"'<>]+@[^\s@\"'<>]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"\+\d[\d \-]{6,}\d|\b\d{10,}\b")  # not dates
# LetsFG's explicit "passport for everyone" rule in a need_info message.
_PASSPORT_EVERYONE_RE = re.compile(
    r"passport\s+is\s+(?:needed|required)\s+for\s+(?:every|each|all)\s+travell?ers?", re.I
)


def _scrub(text: str, passenger: dict) -> str:
    """Remove passenger values (passport number, DOB, address, …) from a
    server message, plus anything shaped like an e-mail or phone number."""
    out = str(text or "")
    values = sorted({str(v) for v in passenger.values()}, key=len, reverse=True)
    for v in values:
        if len(v) >= 3 and v.lower() not in ("adult",):
            out = out.replace(v, "[redacted]")
    out = _EMAIL_RE.sub("[redacted]", out)
    return _PHONE_RE.sub("[redacted]", out)


def passport_required_for_everyone(message) -> bool:
    """True if LetsFG says a passport is needed for every traveller."""
    return bool(_PASSPORT_EVERYONE_RE.search(str(message or "")))


def _error_result(e: RestError, *, phase: str) -> dict:
    code = e.code
    missing = parse_missing_fields(e.body)
    if e.status_code in (400, 422) and (missing or code == "need_info"):
        return {
            "status": "need_info",
            "missing_fields": missing,
            "message": "LetsFG needs more passenger details: "
            + (", ".join(missing) or "unspecified fields")
            + ".",
        }
    if e.status_code == 402 and (e.body.get("pay_url") or isinstance(e.body.get("retry"), dict)):
        kind = "payment_required" if code == "payment_method_required" else "payment_declined"
        return payment_link(e.body, kind=kind)
    if e.status_code == 402 and code == "payment_declined":
        return {
            "status": "failed",
            "payment_declined": True,
            "message": _status_message(e.body, "failed") or "The payment was declined. Nothing was charged.",
        }
    if e.status_code == 409 and code == "too_many_attempts":
        return {
            "status": "payment_blocked",
            "message": e.text
            or "The payment was declined 5 times for this booking attempt. Nothing was charged.",
        }
    if e.status_code == 409 or "price" in code.lower():
        return {"status": "failed", "price_changed": True, "message": "The fare's price changed on LetsFG."}
    if e.status_code == 410 or "expired" in code.lower():
        return {
            "status": "failed",
            "offer_expired": True,
            "message": "Search or offer expired on LetsFG. Run /traveldesk again.",
        }
    if e.status_code == 400 and e.body.get("invalid_fields"):
        fields = e.body["invalid_fields"]
        if isinstance(fields, dict):
            fields = list(fields)
        names = ", ".join(map(str, fields)) if isinstance(fields, list) else "passenger details"
        # Field names only: the server's message may echo the submitted value.
        out = {"status": "need_info", "message": f"LetsFG rejected these traveller details: {names}."}
        if isinstance(fields, list) and "gender" in map(_normalize_field, fields):
            out["message"] += (
                " If your passport shows sex X, this seller or airline may not accept X yet: "
                "ask the travel admin to book it with the airline directly."
            )
        return out
    if e.status_code == 401:
        return {
            "status": "setup_required",
            "message": "LetsFG rejected the API key (401). Check LETSFG_API_KEY.",
        }
    if e.status_code == 402 and code == "payment_method_required":
        return {
            "status": "setup_required",
            "message": "LetsFG account has no payment method. An admin must "
            "connect one (POST /agents/connect-payment) — the bot never does this.",
        }
    if e.status_code == 404:
        return {
            "status": "failed",
            "offer_expired": True,
            "message": "Search or offer expired on LetsFG. Run /traveldesk again.",
        }
    msg = e.text or str(e)
    return {"status": "failed", "message": f"LetsFG {phase} failed ({e.status_code} {code}): {msg}"[:400]}
