"""Tests for the deterministic LetsFG MCP booking adapter.

Covers:
  * Successful book_flight → awaiting_approval + booking_ref.
  * Dev-API-key error → setup_required with a helpful message, never success.
  * Polling get_flight_booking through booking_in_progress → completed w/ PNR.
  * `completed` without a PNR is downgraded to `needs_attention` (never report
    success without a confirmed PNR).
  * Idempotency: a second confirm() for the same booking_id does not re-book.
  * Missing traveller details short-circuit with status need_info.

No real network calls — mcp_client.MCPClient is monkeypatched out.
"""
from __future__ import annotations

from types import SimpleNamespace

import db
import letsfg_booking
import mcp_client


class _FakeMCP:
    """Captures every tool call and returns canned MCP results."""

    def __init__(self, scripts):
        self.scripts = scripts  # {tool_name: [result_or_exc, ...]}
        self.calls = []

    def call_tool(self, name, args):
        self.calls.append({"name": name, "args": args})
        if name not in self.scripts or not self.scripts[name]:
            raise AssertionError(f"unexpected tool call {name}")
        nxt = self.scripts[name].pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def _make_mcp_result(data):
    """Shape of a well-formed MCP tool result (no isError, structuredContent)."""
    return {"isError": False, "content": [], "structuredContent": data}


# --- book_flight ---------------------------------------------------------


def test_book_flight_success(monkeypatch):
    fake = _FakeMCP({
        "book_flight": [_make_mcp_result({
            "booking_ref": "BK-OK",
            "approve_url": "https://pay/BK-OK",
            "status": "awaiting_approval",
        })],
    })
    monkeypatch.setattr(letsfg_booking, "_mcp", lambda: fake)

    out = letsfg_booking.book_flight(
        "S", "O",
        {"given_name": "Raj", "family_name": "Gupta",
         "email": "raj@x", "phone": "+91"},
    )
    assert out["status"] == "awaiting_approval"
    assert out["booking_ref"] == "BK-OK"
    assert out["approve_url"] == "https://pay/BK-OK"
    # Real passenger payload was sent — not an LLM-fabricated one.
    args = fake.calls[0]["args"]
    assert args["search_id"] == "S"
    assert args["offer_id"] == "O"
    assert args["contact_email"] == "raj@x"
    assert args["passengers"][0]["given_name"] == "Raj"
    assert args["passengers"][0]["family_name"] == "Gupta"


def test_book_flight_developer_key_error_is_setup_required(monkeypatch):
    err = mcp_client.MCPError(
        "book_flight reported error: tool requires a traveller card connected "
        "via letsfg.co/connect, which isn't available with the current API "
        "key. Developer API keys must use the separate "
        "/developers/api/v1/flights/book endpoint."
    )
    fake = _FakeMCP({"book_flight": [err]})
    monkeypatch.setattr(letsfg_booking, "_mcp", lambda: fake)

    out = letsfg_booking.book_flight(
        "S", "O",
        {"given_name": "Raj", "family_name": "Gupta", "email": "raj@x"},
    )
    assert out["status"] == "setup_required"
    assert "Developer API key" in out["message"]
    assert "letsfg.co/connect" in out["message"]
    # Must never report success for this condition.
    assert "booking_ref" not in out


def test_book_flight_missing_traveller_details():
    out = letsfg_booking.book_flight("S", "O",
                                     {"given_name": "Raj", "email": "raj@x"})
    assert out["status"] == "need_info"
    assert "family_name" in out["message"]


def test_book_flight_missing_search_id():
    out = letsfg_booking.book_flight(
        "", "O",
        {"given_name": "A", "family_name": "B", "email": "a@b"},
    )
    assert out["status"] == "failed"


# --- get_flight_booking --------------------------------------------------


def test_get_flight_booking_in_progress_then_completed(monkeypatch):
    fake = _FakeMCP({
        "get_flight_booking": [
            _make_mcp_result({"status": "booking_in_progress"}),
            _make_mcp_result({
                "status": "completed", "pnr": "ABC123",
                "total": 4200, "currency": "INR",
                "ticket_url": "https://t/ABC123",
            }),
        ],
    })
    monkeypatch.setattr(letsfg_booking, "_mcp", lambda: fake)

    first = letsfg_booking.get_flight_booking("BK-OK")
    assert first["status"] == "booking_in_progress"
    assert first["pnr"] is None

    second = letsfg_booking.get_flight_booking("BK-OK")
    assert second["status"] == "completed"
    assert second["pnr"] == "ABC123"
    assert second["total"] == 4200
    assert second["ticket_url"] == "https://t/ABC123"


def test_get_flight_booking_completed_without_pnr_is_needs_attention(monkeypatch):
    """Safety: never claim a booking is complete without a confirmed PNR."""
    fake = _FakeMCP({
        "get_flight_booking": [_make_mcp_result({"status": "completed"})],
    })
    monkeypatch.setattr(letsfg_booking, "_mcp", lambda: fake)
    out = letsfg_booking.get_flight_booking("BK-OK")
    assert out["status"] == "needs_attention"
    assert out["pnr"] is None


# --- Idempotency via claude_agent.confirm --------------------------------


def test_confirm_is_idempotent_for_same_booking_id(monkeypatch):
    """Second call to confirm() for a row with an existing booking_ref must
    not invoke book_flight again — this is the real-world duplicate-click /
    resume-after-restart protection."""
    import os
    os.environ["AI_PROVIDER"] = "claude"
    import sys
    sys.modules.pop("config", None)
    sys.modules.pop("claude_agent", None)
    import claude_agent  # fresh

    monkeypatch.setattr(claude_agent.config, "DRY_RUN", False)
    row_id, _ = db.new_request("UEMP1", "DEL BOM idem", "claude")
    db.update(row_id, status="confirmed")

    call_count = {"n": 0}

    def fake_book(search_id, offer_id, traveller):
        call_count["n"] += 1
        return {"status": "awaiting_approval", "booking_ref": "BK-IDEM",
                "approve_url": "https://pay/BK-IDEM", "message": ""}

    monkeypatch.setattr(claude_agent.letsfg_booking, "book_flight", fake_book)

    picked = {"status": "picked", "search_id": "S", "offer_id": "O",
              "summary": "x", "price": 100, "currency": "INR"}
    traveller = {"given_name": "A", "family_name": "B",
                 "email": "a@b", "phone": "+1"}

    out1 = claude_agent.confirm(picked, traveller, booking_id=row_id)
    assert out1["status"] == "awaiting_approval"
    assert call_count["n"] == 1

    # Simulate app.py writing the booking_ref back to the row after the first
    # confirm (same thing `run_booking` does on success).
    db.update(row_id, booking_ref="BK-IDEM", status="awaiting_approval")

    # Second call — must NOT re-invoke book_flight.
    out2 = claude_agent.confirm(picked, traveller, booking_id=row_id)
    assert call_count["n"] == 1
    assert out2["booking_ref"] == "BK-IDEM"
    assert "already issued" in out2["message"]

    # Reset provider for other tests.
    os.environ["AI_PROVIDER"] = "groq"
    sys.modules.pop("config", None)
    sys.modules.pop("claude_agent", None)
    sys.modules.pop("agent", None)


def test_claim_booking_start_only_fires_once():
    row_id, _ = db.new_request("UEMP1", "DEL CCU claim", "claude")
    db.update(row_id, status="confirmed")
    assert db.claim_booking_start(row_id) is True
    # Second attempt must fail — the status is no longer 'confirmed'.
    assert db.claim_booking_start(row_id) is False
