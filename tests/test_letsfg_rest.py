"""LetsFG Developer REST API integration tests.

HTTP is intercepted with httpx.MockTransport — no request leaves the machine,
no real booking or payment is ever made.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest

from traveldesk.booking import letsfg_booking
from traveldesk.clients import letsfg_rest
from traveldesk.storage import db

# Synthetic fixture for tests only — not a real person or document.
TRAVELLER = {
    "given_name": "Raj",
    "family_name": "Gupta",
    "email": "raj@example.com",
    "phone": "+919800000000",
    "gender": "Male",
    "date_of_birth": "1990-01-02",
    "nationality": "IN",
    "passport_number": "Z9081726",
    "passport_country": "IN",
    "passport_expiry": "2032-05-01",
}


class FakeAPI:
    """Scripted LetsFG API. `routes` maps (method, path) → list of (status, body)."""

    def __init__(self, routes):
        self.routes = {k: list(v) for k, v in routes.items()}
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = (request.method, request.url.path.replace("/developers/api/v1", ""))
        script = self.routes.get(key)
        if not script:
            raise AssertionError(f"unexpected call {key}")
        status, body = script.pop(0) if len(script) > 1 else script[0]
        return httpx.Response(status, json=body)

    def bodies(self, path):
        return [json.loads(r.content) for r in self.requests if r.url.path.endswith(path) and r.content]


@pytest.fixture
def api(monkeypatch):
    def install(routes):
        fake = FakeAPI(routes)
        client = httpx.Client(
            base_url="https://letsfg.co/developers/api/v1",
            headers={"X-API-Key": "dev-key-test"},
            transport=httpx.MockTransport(fake.handler),
        )
        monkeypatch.setattr(letsfg_rest, "_http_singleton", client)
        monkeypatch.setattr(letsfg_rest.time, "sleep", lambda _s: None)
        return fake

    yield install
    letsfg_rest._http_singleton = None


# --- search ----------------------------------------------------------------


def test_search_sends_api_key_and_preserves_ids(api):
    fake = api(
        {
            ("POST", "/flights/search"): [
                (
                    200,
                    {
                        "search_id": "SRCH-1",
                        "currency": "INR",
                        "offers": [{"id": "OFF-1", "price_total": 5100, "currency": "INR"}],
                    },
                )
            ]
        }
    )
    out = letsfg_rest.search_flights("DEL", "BOM", "2026-11-20")
    assert out["status"] == "ok"
    assert out["search_id"] == "SRCH-1"
    assert out["offers"][0]["id"] == "OFF-1"
    req = fake.requests[0]
    assert req.headers["X-API-Key"] == "dev-key-test"
    assert "authorization" not in {k.lower() for k in req.headers}
    body = fake.bodies("/flights/search")[0]
    assert body["origin"] == "DEL" and body["destination"] == "BOM"
    assert body["date_from"] == "2026-11-20"


def test_search_without_search_id_is_not_bookable(api):
    api({("POST", "/flights/search"): [(200, {"offers": [{"id": "OFF-1"}]})]})
    out = letsfg_rest.search_flights("DEL", "BOM", "2026-11-20")
    assert out["status"] == "failed"


# --- book ------------------------------------------------------------------


def test_book_maps_passenger_and_sends_idempotency_key(api):
    fake = api(
        {
            ("POST", "/flights/book"): [
                (
                    202,
                    {
                        "ok": True,
                        "booking_id": "BKG-1",
                        "state": "authorised",
                        "charged": 0,
                    },
                )
            ]
        }
    )
    out = letsfg_rest.book_flight("SRCH-1", "OFF-1", TRAVELLER, "td-7-abc")
    assert out["status"] == "booking_in_progress"
    assert out["booking_ref"] == "BKG-1"
    assert "pnr" not in out  # nothing claimed at book time

    body = fake.bodies("/flights/book")[0]
    assert body["search_id"] == "SRCH-1" and body["offer_id"] == "OFF-1"
    assert body["idempotency_key"] == "td-7-abc"
    assert body["contact_email"] == "raj@example.com"
    p = body["passengers"][0]
    assert p["given_name"] == "Raj" and p["family_name"] == "Gupta"
    assert p["phone_number"] == "+919800000000"
    assert "phone" not in p
    assert p["gender"] == "m"
    assert p["born_on"] == "1990-01-02"


def test_book_retries_transient_error_with_same_idempotency_key(api):
    fake = api(
        {
            ("POST", "/flights/book"): [
                (503, {"error": "temporarily_unavailable"}),
                (202, {"ok": True, "booking_id": "BKG-2", "state": "authorised"}),
            ]
        }
    )
    out = letsfg_rest.book_flight("S", "O", TRAVELLER, "td-9-key")
    assert out["booking_ref"] == "BKG-2"
    keys = [b["idempotency_key"] for b in fake.bodies("/flights/book")]
    assert keys == ["td-9-key", "td-9-key"]


def test_book_missing_fields_is_need_info_without_leaking_values(api):
    api(
        {
            ("POST", "/flights/book"): [
                (
                    400,
                    {
                        "error": "missing_fields",
                        "missing_fields": ["passport_number", "born_on"],
                    },
                )
            ]
        }
    )
    out = letsfg_rest.book_flight("S", "O", TRAVELLER, "k")
    assert out["status"] == "need_info"
    assert "passport_number" in out["message"]
    assert "raj@example.com" not in out["message"]
    assert "booking_ref" not in out


def test_book_payment_method_required_is_setup_required(api):
    api({("POST", "/flights/book"): [(402, {"error": "payment_method_required"})]})
    out = letsfg_rest.book_flight("S", "O", TRAVELLER, "k")
    assert out["status"] == "setup_required"
    assert "never does this" in out["message"]


def test_book_declined_is_failed(api):
    api({("POST", "/flights/book"): [(402, {"error": "payment_declined", "message": "Card declined"})]})
    out = letsfg_rest.book_flight("S", "O", TRAVELLER, "k")
    assert out["status"] == "failed"
    assert "booking_ref" not in out


# --- status ----------------------------------------------------------------


@pytest.mark.parametrize(
    "body,status,pnr,charged",
    [
        (
            {
                "state": "completed",
                "pnr": "PNR123",
                "charged_amount": 5234.5,
                "held_amount": 5300,
                "currency": "INR",
            },
            "completed",
            "PNR123",
            5234.5,
        ),
        ({"state": "completed", "pnr": None, "charged_amount": 5234.5}, "needs_attention", None, None),
        ({"state": "booking_in_progress", "held_amount": 5300}, "booking_in_progress", None, None),
        ({"state": "card_issued"}, "booking_in_progress", None, None),
        ({"state": "failed", "failure_reason": "fare gone"}, "failed", None, None),
        ({"state": "needs_attention", "question": "Confirm name spelling"}, "needs_attention", None, None),
    ],
)
def test_status_mapping(api, body, status, pnr, charged):
    api({("GET", "/flights/bookings/BKG-1"): [(200, body)]})
    out = letsfg_rest.get_booking("BKG-1")
    assert out["status"] == status
    assert out["pnr"] == pnr
    assert out["charged_amount"] == charged


def test_status_awaiting_payment_exposes_pay_url(api):
    api(
        {
            ("GET", "/flights/bookings/BKG-1"): [
                (200, {"state": "awaiting_payment", "pay_url": "https://letsfg.co/pay/x"})
            ]
        }
    )
    out = letsfg_rest.get_booking("BKG-1")
    assert out["status"] == "awaiting_approval"
    assert out["approve_url"] == "https://letsfg.co/pay/x"


def test_status_transient_error_keeps_polling(api):
    api({("GET", "/flights/bookings/BKG-1"): [(503, {"error": "temporarily_unavailable"})]})
    assert letsfg_rest.get_booking("BKG-1")["status"] == "booking_in_progress"


# --- dispatch + idempotency key ---------------------------------------------


def test_rest_pick_books_via_rest_with_stable_row_key(monkeypatch):
    row_id, request_key = db.new_request("UEMP1", "DEL BOM rest key", "claude")
    calls = []

    def fake_book(search_id, offer_id, traveller, key):
        calls.append(key)
        return {"status": "booking_in_progress", "booking_ref": "BKG-K"}

    monkeypatch.setattr(letsfg_booking.letsfg_rest, "book_flight", fake_book)
    picked = {"backend": "rest", "search_id": "S", "offer_id": "O"}
    letsfg_booking.book(picked, TRAVELLER, row_id)
    letsfg_booking.book(picked, TRAVELLER, row_id)
    assert calls[0] == calls[1] == f"td-{row_id}-{request_key}"
    assert len(calls[0]) <= 64

    other_id, _ = db.new_request("UEMP1", "DEL BOM rest key 2", "claude")
    letsfg_booking.book(picked, TRAVELLER, other_id)
    assert calls[2] != calls[0]


@pytest.mark.parametrize("backend", ["mcp", None])
def test_non_rest_pick_is_refused_without_any_call(monkeypatch, backend):
    """Picks from the retired MCP flow carry IDs REST can't book."""
    monkeypatch.setattr(
        letsfg_booking.letsfg_rest,
        "book_flight",
        lambda *a: (_ for _ in ()).throw(AssertionError("REST used")),
    )
    out = letsfg_booking.book({"backend": backend, "search_id": "S", "offer_id": "O"}, TRAVELLER, None)
    assert out["status"] == "failed" and "send the request again" in out["message"]
    assert "booking_ref" not in out


def test_rest_missing_traveller_details_never_calls_api(monkeypatch):
    monkeypatch.setattr(
        letsfg_booking.letsfg_rest,
        "book_flight",
        lambda *a: (_ for _ in ()).throw(AssertionError("API called")),
    )
    out = letsfg_booking.book(
        {"backend": "rest", "search_id": "S", "offer_id": "O"}, {"given_name": "Raj", "email": "r@x"}, None
    )
    assert out["status"] == "need_info"


# --- end-to-end poll through app.watch_booking ------------------------------


def _watch(monkeypatch, api, status_script):
    from traveldesk.agents import claude_agent
    from traveldesk.slack import app as bot_app

    api({("GET", "/flights/bookings/BKG-E2E"): status_script})
    monkeypatch.setattr(bot_app.config, "POLL_SECONDS", 0)
    monkeypatch.setattr(bot_app.config, "POLL_MAX_MINUTES", 1)
    monkeypatch.setattr(bot_app.agent_factory, "get_agent", lambda: claude_agent)
    row_id, _ = db.new_request("UEMP1", "E2E rest poll", "claude")
    db.update(
        row_id,
        status="booking_in_progress",
        booking_ref="BKG-E2E",
        details={"backend": "rest", "search_id": "S", "offer_id": "O"},
    )
    client = MagicMock()
    bot_app.watch_booking(client, row_id, "CDM", "BKG-E2E")
    texts = [c.kwargs.get("text", "") for c in client.chat_postMessage.call_args_list]
    return db.get(row_id), texts


def test_poll_until_completed_saves_pnr_and_charged_amount(monkeypatch, api):
    row, texts = _watch(
        monkeypatch,
        api,
        [
            (200, {"state": "authorised", "held_amount": 5300}),
            (200, {"state": "booking_in_progress", "held_amount": 5300}),
            (200, {"state": "completed", "pnr": "XYZ789", "charged_amount": 5234.5, "currency": "INR"}),
        ],
    )
    assert row["status"] == "completed"
    assert row["pnr"] == "XYZ789"
    assert row["charged_amount"] == 5234.5
    assert row["payment_status"] == "paid"
    assert any("XYZ789" in t for t in texts)


def test_poll_completed_without_pnr_never_reports_success(monkeypatch, api):
    row, texts = _watch(
        monkeypatch,
        api,
        [
            (200, {"state": "completed", "charged_amount": 5234.5}),
        ],
    )
    assert row["status"] == "needs_attention"
    assert row["pnr"] is None
    assert row["charged_amount"] is None
    assert row["payment_status"] != "paid"
    assert not any("Ticket issued" in t for t in texts)


def test_poll_failed_saves_nothing(monkeypatch, api):
    row, texts = _watch(
        monkeypatch,
        api,
        [
            (200, {"state": "booking_in_progress"}),
            (200, {"state": "failed", "failure_reason": "Airline rejected"}),
        ],
    )
    assert row["status"] == "failed"
    assert row["pnr"] is None
    assert row["charged_amount"] is None
    assert any("Airline rejected" in t for t in texts)


# --- Claude REST pick validation --------------------------------------------


def _claude(monkeypatch, offers, choice, max_price=None):
    from traveldesk.agents import claude_agent

    monkeypatch.setattr(claude_agent.config, "MAX_PRICE", max_price)
    monkeypatch.setattr(
        claude_agent.letsfg_rest,
        "search_flights",
        lambda *a, **k: {"status": "ok", "search_id": "SRCH-9", "offers": offers, "currency": "INR"},
    )
    replies = iter(
        [
            {
                "status": "ok",
                "origin": "DEL",
                "destination": "BOM",
                "date_from": "2026-11-20",
                "return_from": None,
            },
            choice,
        ]
    )
    monkeypatch.setattr(claude_agent, "_plain_json", lambda *a, **k: next(replies))
    return claude_agent.pick("DEL to BOM 20 Nov", TRAVELLER, booking_id=None)


def test_claude_rest_pick_preserves_search_and_offer_ids(monkeypatch):
    out = _claude(
        monkeypatch,
        [{"id": "OFF-A", "price_total": 6000}, {"id": "OFF-B", "price_total": 5000}],
        {"status": "picked", "offer_id": "OFF-B", "summary": "6E · DEL → BOM"},
    )
    assert out["status"] == "picked"
    assert out["backend"] == "rest"
    assert out["search_id"] == "SRCH-9"
    assert out["offer_id"] == "OFF-B"
    assert out["price"] == 5000


def test_claude_rest_pick_rejects_invented_offer(monkeypatch):
    out = _claude(
        monkeypatch, [{"id": "OFF-A", "price_total": 6000}], {"status": "picked", "offer_id": "OFF-FAKE"}
    )
    assert out["status"] == "failed"


def test_claude_rest_pick_enforces_max_price(monkeypatch):
    out = _claude(
        monkeypatch,
        [{"id": "OFF-A", "price_total": 20000}],
        {"status": "picked", "offer_id": "OFF-A"},
        max_price=15000,
    )
    assert out["status"] == "no_match"
    assert "above MAX_PRICE" in out["message"] and "20000" in out["message"]


def test_claude_rest_pick_unreadable_choice_is_failed_not_no_match(monkeypatch):
    out = _claude(
        monkeypatch,
        [{"id": "OFF-A", "price_total": 6000}],
        {"status": "failed", "message": "Claude's pick answer was cut off at 2000 tokens."},
    )
    assert out["status"] == "failed"
    assert "cut off" in out["message"]


def test_plain_json_reports_truncation(monkeypatch):
    from traveldesk.agents import claude_agent

    resp = SimpleNamespace(
        content=[
            SimpleNamespace(type="text", text='{"status": "picked", "offer_id": "wo_1", "message": "Cheap')
        ],
        stop_reason="max_tokens",
        usage=SimpleNamespace(
            input_tokens=1, output_tokens=2000, cache_read_input_tokens=0, cache_creation_input_tokens=0
        ),
    )
    monkeypatch.setattr(
        claude_agent, "_client", lambda: SimpleNamespace(messages=SimpleNamespace(create=lambda **k: resp))
    )
    out = claude_agent._plain_json("m", "s", "p", task_kind="pick", booking_id=None)
    assert out["status"] == "failed" and "cut off at 2000 tokens" in out["message"]


def test_claude_rest_pick_no_offers_names_route_not_price(monkeypatch):
    out = _claude(monkeypatch, [], {"status": "picked", "offer_id": "X"}, max_price=5e10)
    assert out["status"] == "no_match"
    assert "DEL → BOM on 2026-11-20" in out["message"]
    assert "MAX_PRICE" not in out["message"]
